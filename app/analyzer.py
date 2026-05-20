"""
Claude-powered stock analysis engine with handoff-note memory system.
Uses the Claude Agent SDK so analysis runs against the user's Claude
subscription rather than a separate API key.
"""
import asyncio
import json
import logging
import re
import os
import threading
from datetime import datetime, timezone
from typing import Optional

import yfinance as yf
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from .analysis_errors import AnalysisQuotaError, classify_analysis_error
from .currency import enrich_position_currencies, format_money, normalize_currency
from .database import Database
from .prices import _ticker_candidates
from .ticker_resolve import resolve_market_ticker

_STALE_HANDOFF_MARKERS = (
    "$0", "0.0000", "worthless", "total loss", "illiquid",
    "no market", "zero probability", "confirmed total loss",
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are an expert equity analyst AI assistant. Your job is to analyse individual stock positions in a user's portfolio and return a structured JSON recommendation.

Output ONLY a valid JSON object — no markdown fences, no preamble, no explanation outside the JSON.

Recommendation values and their meaning:
- BUY        = add to or open this position now
- SELL       = exit or reduce this position now
- HOLD_LONG  = hold with conviction — long-term thesis intact, do not sell despite short-term volatility
- HOLD_SHORT = hold cautiously — reassess soon, consider reducing if thesis weakens
- WATCH      = not currently held but worth monitoring for a future entry

JSON schema (all fields required):
{
  "recommendation": "BUY" | "SELL" | "HOLD_LONG" | "HOLD_SHORT" | "WATCH",
  "price_target_30d": <number>,
  "price_target_range": [<lo number>, <hi number>],
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "reasoning": "<2-3 sentence core thesis>",
  "news_sentiment": "POSITIVE" | "NEUTRAL" | "NEGATIVE",
  "news_summary": "<1-2 sentences on recent news>",
  "catalysts": ["<catalyst 1>", "<catalyst 2>", "<catalyst 3>"],
  "risks": ["<risk 1>", "<risk 2>", "<risk 3>"],
  "worries": ["<worry 1>", "<worry 2>"],
  "outlook_90d": "<1 sentence 90-day view>",
  "handoff_note": {
    "thesis_summary": "<one-sentence core thesis for next run>",
    "watch_items": ["<item 1>", "<item 2>", "<item 3>"],
    "trend_flags": [],
    "ongoing_risks": ["<risk 1>", "<risk 2>"],
    "ongoing_catalysts": ["<catalyst 1>", "<catalyst 2>"]
  },
  "knowledge_notes": []
}

knowledge_notes (optional array — use [] when nothing qualifies):
Each item is durable, reusable insight for the user's Obsidian knowledge base (not this trade alone):
{
  "slug": "<kebab-case-id, e.g. tmc-soac-t212-ticker-map>",
  "title": "<short note title>",
  "body": "<2-5 sentences markdown — fact/framework/mapping the user should remember>",
  "mocs": ["MOC-uk-reits"]
}
The app always links every note to MOC-investment-analysis (and creates it if missing). Add extra mocs only for clear topic fit (e.g. MOC-uk-reits for REIT holdings).

When to add knowledge_notes (be selective — most runs should use []):
- Ticker/identity mapping (e.g. T212 legacy SPAC code vs live market symbol)
- Sector or instrument-type insight that applies beyond one position
- A material change to investment thesis worth remembering long-term
- Correction of a prior misconception (e.g. delisted shell vs post-merger equity)

Do NOT add knowledge_notes for: routine HOLD/BUY recaps, price targets, P&L, or text that only belongs in the run report.

Guidelines:
- Base targets on realistic near-term fundamentals, not wishful thinking.
- Prefer HOLD_LONG over HOLD when you have high conviction the thesis is intact.
- Prefer HOLD_SHORT over HOLD when near-term risk is elevated or the thesis is weakening.
- trend_flags: note if your recommendation differs from the previous run (e.g. "HOLD_LONG→SELL").
- watch_items: be specific and actionable — include earnings dates, product launches, macro events.
- Use fundamentals (P/E vs sector, EPS growth, analyst consensus) alongside price action.
- Trading 212 position figures (avg cost, current price, value, P&L) are in the ACCOUNT currency (e.g. GBP).
- yfinance / analyst targets may be in the LISTING currency (e.g. USD) — label them clearly; never mix currencies in one comparison without stating both."""


class StockAnalyzer:
    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        self._run_id: Optional[int] = None
        self._status: str = "idle"   # idle | running | done | error
        self._progress: list[str] = []
        self._current_ticker: Optional[str] = None
        self._error_kind: Optional[str] = None
        self._error_message: Optional[str] = None
        self._ideas_lock = threading.Lock()
        self._ideas_status: str = "idle"
        self._ideas_progress: list[str] = []
        self._ideas_error_kind: Optional[str] = None
        self._ideas_error_message: Optional[str] = None

    # ── Status (thread-safe) ───────────────────────────────────────────────────

    @property
    def status(self) -> dict:
        with self._lock:
            return {
                "status": self._status,
                "run_id": self._run_id,
                "progress": list(self._progress),
                "current_ticker": self._current_ticker,
                "error_kind": self._error_kind,
                "error_message": self._error_message,
            }

    def _set_status(
        self,
        status: str,
        error_kind: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._status = status
            if status != "running":
                self._current_ticker = None
            if status == "error":
                self._error_kind = error_kind
                self._error_message = error_message
            elif status in ("idle", "running", "done"):
                self._error_kind = None
                self._error_message = None

    def _set_current_ticker(self, ticker: Optional[str]) -> None:
        with self._lock:
            self._current_ticker = ticker

    def _log(self, msg: str) -> None:
        logger.info(msg)
        with self._lock:
            self._progress.append(msg)
        if self._run_id:
            self.db.append_log(self._run_id, msg)

    # ── Main entry point ───────────────────────────────────────────────────────

    def run_analysis(
        self,
        holdings: list[dict],
        *,
        generate_reports: bool = True,
    ) -> int:
        """
        Analyse the given holdings. Returns the run_id.
        Designed to be called in a background thread.
        """
        if self._status == "running":
            raise RuntimeError("Analysis already in progress")

        run_id = self.db.create_run()
        account_currency = self.db.get_account_currency()
        with self._lock:
            self._run_id = run_id
            self._status = "running"
            self._progress = []
            self._current_ticker = None
            self._error_kind = None
            self._error_message = None

        run_scope = "single" if len(holdings) == 1 else "full"
        if run_scope == "single":
            self._log(f"[{_ts()}] Single-ticker analysis: {holdings[0]['ticker']}")
        else:
            self._log(f"[{_ts()}] Starting analysis for {len(holdings)} holdings")

        saved_count = 0
        quota_message: Optional[str] = None

        try:
            for holding in holdings:
                ticker = holding["ticker"]
                self._set_current_ticker(ticker)
                try:
                    self._log(f"[{_ts()}] Analysing {ticker}…")
                    enrich_position_currencies(holding, account_currency)
                    result = self._analyse_ticker(holding, account_currency)
                    _persist_knowledge_notes(
                        run_id, holding, result.pop("knowledge_notes", None), self._log,
                    )
                    self.db.save_analysis(run_id, ticker, result)
                    saved_count += 1
                    if "handoff_note" in result:
                        self.db.save_handoff_note(ticker, result["handoff_note"])
                    self._log(f"[{_ts()}] {ticker} → {result.get('recommendation', '?')}")
                except AnalysisQuotaError as exc:
                    quota_message = str(exc)
                    self._log(f"[{_ts()}] Stopping run — {quota_message}")
                    break
                except Exception as exc:
                    kind, user_msg = classify_analysis_error(exc)
                    self._log(f"[{_ts()}] ERROR {ticker}: {exc}")
                    logger.exception("Analysis failed for %s", ticker)
                    if kind == "quota":
                        quota_message = user_msg
                        self._log(f"[{_ts()}] Stopping run — {user_msg}")
                        break

            self._set_current_ticker(None)

            if quota_message:
                self.db.finish_run(run_id, "error", saved_count)
                self._log(f"[{_ts()}] Analysis stopped: {quota_message}")
                self._set_status("error", "quota", quota_message)
            else:
                self.db.finish_run(run_id, "done", saved_count)
                self._log(f"[{_ts()}] Analysis complete — saving reports…")
                if generate_reports:
                    _generate_run_reports(self.db, run_id, self._log, run_scope)
                else:
                    _generate_obsidian_report(self.db, run_id, self._log, run_scope)
                self._log(f"[{_ts()}] All report steps finished.")
                self._set_status("done")

        except Exception as exc:
            self._set_current_ticker(None)
            kind, user_msg = classify_analysis_error(exc)
            self.db.finish_run(run_id, "error", saved_count)
            self._log(f"[{_ts()}] Fatal error: {exc}")
            self._set_status("error", kind, user_msg)
            logger.exception("Fatal error during analysis run")

        return run_id

    # ── Per-ticker analysis ────────────────────────────────────────────────────

    def _analyse_ticker(self, holding: dict, account_currency: str) -> dict:
        ticker = holding["ticker"]
        acct_ccy = normalize_currency(account_currency)

        if holding.get("is_pie"):
            self._log(f"[{_ts()}] {ticker}: pie portfolio — aggregated analysis")
            market_data = {
                "ticker": ticker,
                "current_price": holding.get("current_price"),
            }
        else:
            market_ticker = _ensure_market_ticker(holding, self.db)
            if market_ticker != ticker:
                self._log(
                    f"[{_ts()}] {ticker}: fetching market data as {market_ticker} "
                    f"(T212 code {ticker})"
                )
            else:
                self._log(f"[{_ts()}] {ticker}: fetching market data from yfinance")
            market_data = _fetch_market_data(market_ticker)
            market_data["t212_ticker"] = ticker
            market_data["market_ticker"] = market_ticker
            _merge_t212_price(holding, market_data)
            if market_data.get("quote_currency"):
                holding["quote_currency"] = market_data["quote_currency"]
                enrich_position_currencies(holding, acct_ccy, quote_currency=holding["quote_currency"])

        price = market_data.get("current_price")
        pe = market_data.get("pe_ratio")
        analyst_t = market_data.get("analyst_target_mean")
        analyst_c = market_data.get("analyst_consensus", "")
        earnings = market_data.get("next_earnings")
        eps_g = market_data.get("eps_growth_pct")
        self._log(
            f"[{_ts()}] {ticker}: price={format_money(price, acct_ccy)} (T212, {acct_ccy})  "
            f"P/E={pe}  analyst={analyst_t}({analyst_c})  "
            f"EPS-growth={eps_g}%  earnings={earnings}"
        )

        handoff_note = _handoff_for_analysis(
            self.db.get_handoff_note(ticker),
            holding.get("current_price"),
        )
        if handoff_note:
            thesis = (handoff_note.get("thesis_summary") or "")[:80]
            self._log(f"[{_ts()}] {ticker}: memory loaded — \"{thesis}\"")
        elif self.db.get_handoff_note(ticker) and (holding.get("current_price") or 0) > 0.05:
            self._log(
                f"[{_ts()}] {ticker}: skipped stale handoff (contradicts T212 live price)"
            )

        self._log(f"[{_ts()}] {ticker}: sending to Claude AI for analysis…")
        prompt = _build_prompt(holding, market_data, handoff_note)
        raw = _call_claude_sync(_SYSTEM_PROMPT + "\n\n" + prompt)
        self._log(f"[{_ts()}] {ticker}: Claude response received ({len(raw)} chars)")

        result = _parse_analysis_json(raw)
        rec = result.get("recommendation", "?")
        conf = result.get("confidence", "?")
        target = result.get("price_target_30d")
        sentiment = result.get("news_sentiment", "")
        reasoning_preview = (result.get("reasoning") or "")[:100]
        self._log(
            f"[{_ts()}] {ticker}: → {rec} ({conf})  "
            f"target={target}  sentiment={sentiment}"
        )
        self._log(f"[{_ts()}] {ticker}: \"{reasoning_preview}…\"")

        cats = result.get("catalysts", [])
        risks = result.get("risks", [])
        if cats:
            self._log(f"[{_ts()}] {ticker}: catalysts: {' | '.join(cats[:3])}")
        if risks:
            self._log(f"[{_ts()}] {ticker}: risks: {' | '.join(risks[:3])}")

        # Persist position + fundamental data alongside Claude's output
        result["current_price"]       = market_data.get("current_price")
        result["cost_basis"]          = holding.get("avg_cost")
        result["shares"]              = holding.get("shares")
        result["pe_ratio"]            = market_data.get("pe_ratio")
        result["eps_growth_pct"]      = market_data.get("eps_growth_pct")
        result["analyst_target_mean"] = market_data.get("analyst_target_mean")
        result["analyst_consensus"]   = market_data.get("analyst_consensus")
        result["next_earnings"]       = market_data.get("next_earnings")
        result["account_currency"]    = acct_ccy
        result["instrument_currency"] = holding.get("instrument_currency")
        result["quote_currency"]      = market_data.get("quote_currency")

        # Detect recommendation changes and record as trend flags
        prev = self.db.get_ticker_history(ticker, limit=1)
        if prev:
            prev_rec = prev[0].get("recommendation")
            new_rec = result.get("recommendation")
            if prev_rec and new_rec and prev_rec != new_rec:
                flag = f"{prev_rec}→{new_rec}"
                self._log(f"[{_ts()}] {ticker}: trend change detected — {flag}")
                if "handoff_note" in result:
                    result["handoff_note"].setdefault("trend_flags", []).append(flag)

        return result

    # ── Background runner ──────────────────────────────────────────────────────

    def run_in_background(
        self,
        holdings: list[dict],
        *,
        generate_reports: bool = True,
    ) -> None:
        t = threading.Thread(
            target=self.run_analysis,
            args=(holdings,),
            kwargs={"generate_reports": generate_reports},
            daemon=True,
            name="analyzer",
        )
        t.start()

    # ── Discovery: stock ideas ─────────────────────────────────────────────────

    @property
    def ideas_status(self) -> dict:
        with self._ideas_lock:
            return {
                "status": self._ideas_status,
                "progress": list(self._ideas_progress),
                "error_kind": self._ideas_error_kind,
                "error_message": self._ideas_error_message,
            }

    def _ideas_log(self, msg: str) -> None:
        logger.info(msg)
        with self._ideas_lock:
            self._ideas_progress.append(msg)

    def _set_ideas_status(
        self,
        status: str,
        error_kind: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        with self._ideas_lock:
            self._ideas_status = status
            if status == "error":
                self._ideas_error_kind = error_kind
                self._ideas_error_message = error_message
            elif status in ("idle", "running", "done"):
                self._ideas_error_kind = None
                self._ideas_error_message = None

    def generate_stock_ideas_bg(
        self,
        portfolio_tickers: list[str],
        t212=None,
    ) -> None:
        with self._ideas_lock:
            if self._ideas_status == "running":
                raise RuntimeError("Discovery generation already in progress")
        t = threading.Thread(
            target=self.generate_stock_ideas,
            args=(portfolio_tickers, t212),
            daemon=True,
            name="discovery-ideas",
        )
        t.start()

    def generate_stock_ideas(self, portfolio_tickers: list[str], t212=None) -> dict:
        with self._ideas_lock:
            self._ideas_status = "running"
            self._ideas_progress = []

        self._ideas_log(f"[{_ts()}] Generating discovery ideas…")
        portfolio_set = {t.upper() for t in portfolio_tickers}

        try:
            prompt = _build_ideas_prompt(portfolio_tickers)
            self._ideas_log(f"[{_ts()}] Calling Claude for stock ideas…")
            raw = _call_claude_sync(_IDEAS_SYSTEM_PROMPT + "\n\n" + prompt)
            self._ideas_log(f"[{_ts()}] Claude response received ({len(raw)} chars)")

            parsed = _parse_ideas_json(raw)
            generated_at = datetime.now(timezone.utc).isoformat()
            flat: list[dict] = []

            for cat_key, items in parsed.items():
                cat = _normalize_category(cat_key)
                if not cat:
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    ticker = (item.get("ticker") or "").upper().strip()
                    if not ticker or ticker in portfolio_set:
                        continue
                    price = _fetch_idea_price(ticker)
                    flat.append({
                        "ticker": ticker,
                        "company_name": item.get("company_name") or ticker,
                        "category": cat,
                        "confidence": (item.get("confidence") or "MEDIUM").upper(),
                        "reasoning": item.get("reasoning") or "",
                        "catalysts": item.get("catalysts") or [],
                        "risks": item.get("risks") or [],
                        "price_at_rec": price,
                        "generated_at": generated_at,
                    })

            if flat and t212 is not None:
                from .t212_instruments import enrich_recommendations_t212

                self._ideas_log(f"[{_ts()}] Checking T212 instrument availability…")
                flat, n_t212 = enrich_recommendations_t212(flat, t212)
                self._ideas_log(
                    f"[{_ts()}] T212: {n_t212}/{len(flat)} ideas available to trade"
                )

            self.db.save_stock_recommendations(flat)
            n_cats = len({r["category"] for r in flat})
            self._ideas_log(
                f"[{_ts()}] Saved {len(flat)} ideas"
                + (f" across {n_cats} categories" if n_cats else " (previous batch expired)")
            )
            self._set_ideas_status("done")
            return self.db.get_stock_recommendations()

        except AnalysisQuotaError as exc:
            self._ideas_log(f"[{_ts()}] Stopped — {exc}")
            self._set_ideas_status("error", "quota", str(exc))
            raise
        except Exception as exc:
            kind, user_msg = classify_analysis_error(exc)
            self._ideas_log(f"[{_ts()}] Discovery failed: {exc}")
            self._set_ideas_status("error", kind, user_msg)
            logger.exception("Discovery ideas generation failed")
            raise


# ── Claude Agent SDK helpers ───────────────────────────────────────────────────

_IDEAS_SYSTEM_PROMPT = """You are a stock research analyst helping a UK-based retail investor discover new ideas.
Suggest stocks they do NOT already hold. Output ONLY valid JSON — no markdown fences, no preamble.

Use yfinance-compatible tickers (e.g. AAPL, MSFT, TMC, TM1.L for UK listings).
Provide 3-5 ideas per category. confidence must be HIGH, MEDIUM, or LOW."""



def _normalize_category(key: str) -> Optional[str]:
    k = (key or "").lower().strip().replace(" ", "_")
    mapping = {
        "growth": "growth",
        "value": "value",
        "momentum": "momentum",
        "motion": "momentum",
        "dividend": "dividend",
        "dividend_income": "dividend",
        "defensive": "defensive",
    }
    if k in mapping:
        return mapping[k]
    return k if k in Database._REC_CATEGORIES else None


def _build_ideas_prompt(portfolio_tickers: list[str]) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    held = ", ".join(sorted(portfolio_tickers)) if portfolio_tickers else "(empty portfolio)"
    return f"""Current portfolio (do NOT recommend these): {held}
Date: {today}

Suggest 3-5 NEW stocks per category for a UK investor using Trading 212.
Categories: growth, value, dividend, momentum, defensive

For each stock:
- ticker (yfinance symbol)
- company_name
- confidence: HIGH | MEDIUM | LOW
- reasoning (2-3 sentences)
- catalysts: [2-3 strings]
- risks: [2 strings]

Return JSON only:
{{"growth": [...], "value": [...], "dividend": [...], "momentum": [...], "defensive": [...]}}"""


def _parse_ideas_json(raw: str) -> dict:
    text = raw.strip()
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            chunk = part.strip()
            if chunk.lower().startswith("json"):
                chunk = chunk[4:].strip()
            if chunk.startswith("{"):
                text = chunk
                break
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object from Claude")
    return data


def _fetch_idea_price(ticker: str) -> Optional[float]:
    for candidate in _ticker_candidates(ticker):
        try:
            fast = yf.Ticker(candidate).fast_info
            if fast.last_price and float(fast.last_price) > 0:
                return float(fast.last_price)
        except Exception:
            continue
    return None


# ── Claude Agent SDK helpers (continued) ───────────────────────────────────────

async def _call_claude_async(prompt: str) -> str:
    """Single-turn query via the Claude Agent SDK."""
    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(max_turns=1),
    ):
        if isinstance(message, ResultMessage):
            return (message.result or "").strip()
    return ""


def _call_claude_sync(prompt: str) -> str:
    """Sync wrapper safe to call from any background thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        raw = loop.run_until_complete(_call_claude_async(prompt))
    except Exception as exc:
        kind, user_msg = classify_analysis_error(exc)
        if kind == "quota":
            raise AnalysisQuotaError(user_msg) from exc
        raise
    finally:
        loop.close()

    if not (raw or "").strip():
        raise RuntimeError("Empty response from Claude")
    return raw


def _parse_analysis_json(raw: str) -> dict:
    """
    Parse Claude's analysis payload. The Agent SDK sometimes returns markdown
    fences or brief preamble despite the system prompt asking for raw JSON.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("Empty Claude response")
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")

    candidates: list[str] = [text]
    for pattern in (r"```json\s*([\s\S]*?)```", r"```\s*([\s\S]*?)```"):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            candidates.append(match.group(1).strip())

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    last_err: json.JSONDecodeError | None = None
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError as exc:
            last_err = exc
            try:
                data, _ = json.JSONDecoder().raw_decode(candidate.lstrip())
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError as exc2:
                last_err = exc2

    preview = text[:120].replace("\n", " ")
    raise ValueError(
        f"Could not parse JSON from Claude response ({len(text)} chars). "
        f"Starts with: {preview!r}"
    ) from last_err


# ── Market data ────────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _log_obsidian_diagnostics(gen, log_fn, run_scope: str) -> None:
    for line in gen.obsidian_diagnostic_lines(run_scope):
        log_fn(f"[{_ts()}] {line}")


def _log_obsidian_result(gen, log_fn, path: Optional[str], run_scope: str) -> None:
    if path:
        label = gen.obsidian_target_label(run_scope)
        name = os.path.basename(path)
        log_fn(
            f"[{_ts()}] Obsidian: wrote {name} → {label} "
            f"(container: {path})"
        )
        return
    reason = gen.obsidian_skip_reason() or "unknown error"
    log_fn(f"[{_ts()}] Obsidian: no .md written — {reason}")


def _enrich_analyses_from_positions(db: Database, analyses: list[dict]) -> None:
    account = db.get_account_currency()
    pos_map = {p["ticker"]: p for p in db.get_positions()}
    for a in analyses:
        a.setdefault("account_currency", account)
        if not a.get("instrument_currency"):
            inst = (pos_map.get(a["ticker"]) or {}).get("instrument_currency")
            if inst:
                a["instrument_currency"] = inst


def _generate_run_reports(
    db: Database, run_id: int, log_fn, run_scope: str = "full",
) -> None:
    """Write Excel + text + optional Obsidian reports after a successful analysis run."""
    try:
        analyses = db.get_analyses_for_run(run_id)
        if not analyses:
            log_fn(f"[{_ts()}] Reports: skipped — no analyses saved for run {run_id}")
            return
        _enrich_analyses_from_positions(db, analyses)
        from .reports import ReportGenerator

        gen = ReportGenerator()
        log_fn(f"[{_ts()}] Writing local reports (Excel + text)…")
        xlsx = gen.generate_excel(run_id, analyses, run_scope=run_scope)
        txt = gen.generate_text(run_id, analyses, run_scope=run_scope)
        log_fn(f"[{_ts()}] Local reports: {os.path.basename(xlsx)} | {os.path.basename(txt)}")

        _log_obsidian_diagnostics(gen, log_fn, run_scope)
        if gen.obsidian_enabled():
            target = gen.obsidian_target_label(run_scope)
            log_fn(f"[{_ts()}] Writing Obsidian markdown → {target}")
        md = gen.generate_markdown(run_id, analyses, run_scope=run_scope)
        _log_obsidian_result(gen, log_fn, md, run_scope)
    except Exception as exc:
        logger.exception("Report generation failed for run %s: %s", run_id, exc)
        log_fn(f"[{_ts()}] Report generation failed: {exc}")


def _persist_knowledge_notes(
    run_id: int,
    holding: dict,
    knowledge_notes: Optional[list],
    log_fn,
) -> None:
    if not knowledge_notes:
        return
    try:
        from .reports import ReportGenerator

        gen = ReportGenerator()
        ticker = holding["ticker"]
        log_fn(
            f"[{_ts()}] {ticker}: writing {len(knowledge_notes)} knowledge note(s) "
            f"to {gen.obsidian_knowledge_subdir or '50_Knowledge/notes'}/"
        )
        paths = gen.write_knowledge_notes(
            run_id,
            ticker,
            knowledge_notes,
            market_ticker=holding.get("market_ticker"),
        )
        for p in paths:
            log_fn(f"[{_ts()}] {ticker}: knowledge note → {p}")
    except Exception as exc:
        logger.exception("Knowledge note write failed for run %s", run_id)
        log_fn(f"[{_ts()}] Knowledge note failed: {exc}")


def _generate_obsidian_report(
    db: Database, run_id: int, log_fn, run_scope: str = "single",
) -> None:
    """Obsidian-only export (e.g. after a single-ticker ↻ run)."""
    try:
        analyses = db.get_analyses_for_run(run_id)
        if not analyses:
            log_fn(f"[{_ts()}] Obsidian: skipped — no analysis saved for this run")
            return
        _enrich_analyses_from_positions(db, analyses)
        from .reports import ReportGenerator

        gen = ReportGenerator()
        log_fn(f"[{_ts()}] Saving Obsidian report…")
        _log_obsidian_diagnostics(gen, log_fn, run_scope)
        if gen.obsidian_enabled():
            log_fn(
                f"[{_ts()}] Writing Obsidian markdown → "
                f"{gen.obsidian_target_label(run_scope)}"
            )
        path = gen.generate_markdown(run_id, analyses, run_scope=run_scope)
        _log_obsidian_result(gen, log_fn, path, run_scope)
    except Exception as exc:
        logger.exception("Obsidian report failed for run %s: %s", run_id, exc)
        log_fn(f"[{_ts()}] Obsidian report failed: {exc}")


def _ensure_market_ticker(holding: dict, db: Database) -> str:
    """Resolve yfinance symbol before analysis; persist when it changes."""
    ticker = holding["ticker"]
    market = resolve_market_ticker(
        ticker,
        isin=holding.get("isin"),
        instrument_name=holding.get("instrument_name"),
        reference_price=holding.get("current_price"),
        instrument_currency=holding.get("instrument_currency"),
    )
    if market != (holding.get("market_ticker") or ticker):
        db.update_position_market_ticker(ticker, market)
    holding["market_ticker"] = market
    return market


def _merge_t212_price(holding: dict, market_data: dict) -> None:
    """Prefer T212 wallet price for the position; flag bad yfinance symbol matches."""
    t212 = holding.get("current_price")
    if t212 is None or float(t212) <= 0:
        return
    t212 = float(t212)
    yf = market_data.get("current_price")
    market_data["t212_current_price"] = t212
    if yf and float(yf) > 0:
        ratio = float(yf) / t212
        if ratio < 0.12 or ratio > 8.0:
            market_data["price_source_note"] = (
                f"Public quote ({market_data.get('market_ticker', '?')}) "
                f"≈ {float(yf):.4f} does not match Trading 212 ({t212:.4f}); "
                "use T212 for position valuation."
            )
    market_data["current_price"] = t212


def _handoff_for_analysis(
    handoff: Optional[dict],
    t212_price: Optional[float],
) -> Optional[dict]:
    """Drop handoff memory that assumes worthlessness while T212 shows a live price."""
    if not handoff or not t212_price or float(t212_price) < 0.05:
        return handoff
    blob = " ".join(
        str(handoff.get(k, ""))
        for k in (
            "thesis_summary", "watch_items", "ongoing_risks",
            "ongoing_catalysts", "trend_flags",
        )
    ).lower()
    if any(m in blob for m in _STALE_HANDOFF_MARKERS):
        return None
    return handoff


def _fetch_market_data(ticker: str) -> dict:
    try:
        stock = None
        used_symbol = ticker
        for candidate in _ticker_candidates(ticker):
            stock = yf.Ticker(candidate)
            fast = stock.fast_info
            if fast.last_price and float(fast.last_price) > 0:
                used_symbol = candidate
                break
        if stock is None:
            stock = yf.Ticker(ticker)
            used_symbol = ticker
        fast = stock.fast_info
        info = stock.info  # full info — P/E, EPS, analyst targets, earnings date
        hist = stock.history(period="30d")
        news = stock.news or []

        prices = hist["Close"].tolist() if not hist.empty else []
        current_price = float(fast.last_price) if fast.last_price else (prices[-1] if prices else None)

        # 30-day closes for sparkline (stored in market data, not passed to Claude)
        closes_30d = [round(p, 4) for p in prices]

        # Last 10 closes for prompt context (enough for trend, not too many tokens)
        recent_closes = closes_30d[-10:] if closes_30d else []

        headlines = [
            n.get("content", {}).get("title", n.get("title", ""))
            for n in news[:5]
        ]

        # ── Fundamentals ──────────────────────────────────────────────────────
        pe_ratio = info.get("trailingPE") or info.get("forwardPE")
        eps_ttm = info.get("trailingEps")
        eps_growth = info.get("earningsGrowth") or info.get("revenueGrowth")

        # Analyst consensus
        target_mean = info.get("targetMeanPrice")
        target_high = info.get("targetHighPrice")
        target_low  = info.get("targetLowPrice")
        analyst_count = info.get("numberOfAnalystOpinions")
        recommendation_key = info.get("recommendationKey", "")  # e.g. "buy", "hold"

        # Next earnings date (epoch → ISO string)
        earnings_ts = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
        earnings_date: Optional[str] = None
        if earnings_ts:
            try:
                earnings_date = datetime.fromtimestamp(earnings_ts, tz=timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                pass

        quote_currency = info.get("currency")
        if isinstance(quote_currency, str):
            quote_currency = quote_currency.strip().upper() or None
        else:
            quote_currency = None

        return {
            "ticker": used_symbol,
            "quote_currency": quote_currency,
            "current_price": current_price,
            "week_52_high": float(fast.fifty_two_week_high) if fast.fifty_two_week_high else None,
            "week_52_low":  float(fast.fifty_two_week_low)  if fast.fifty_two_week_low  else None,
            "market_cap":   float(fast.market_cap)          if fast.market_cap           else None,
            "recent_closes": recent_closes,
            "closes_30d":    closes_30d,       # for sparkline endpoint only
            "recent_headlines": headlines,
            # Fundamentals
            "pe_ratio":          round(pe_ratio, 2) if pe_ratio else None,
            "eps_ttm":           round(eps_ttm, 4)  if eps_ttm  else None,
            "eps_growth_pct":    round(eps_growth * 100, 1) if eps_growth else None,
            "analyst_target_mean": round(target_mean, 2) if target_mean else None,
            "analyst_target_high": round(target_high, 2) if target_high else None,
            "analyst_target_low":  round(target_low, 2)  if target_low  else None,
            "analyst_count":       analyst_count,
            "analyst_consensus":   recommendation_key,
            "next_earnings":       earnings_date,
        }
    except Exception as exc:
        logger.warning("yfinance failed for %s: %s", ticker, exc)
        return {"ticker": ticker, "current_price": None}


def _build_prompt(holding: dict, market_data: dict, handoff_note: Optional[dict]) -> str:
    ticker = holding["ticker"]

    if holding.get("is_pie"):
        lines = [f"## Portfolio Investment (Pie): {holding.get('pie_name', ticker)}"]
        lines.append(
            "\nThis is a Trading 212 Pie — a basket of stocks managed together. "
            "Provide a combined recommendation for the pie as a whole, considering "
            "diversification, overlap, and how the constituents work together. "
            "Individual tickers inside the pie should not each get a SELL unless the "
            "whole pie thesis is broken."
        )
        lines.append("\n### Pie constituents")
        acct = normalize_currency(holding.get("account_currency"))
        for m in holding.get("pie_members", []):
            lines.append(
                f"- {m['ticker']}: {m.get('shares', 0):g} shares @ avg "
                f"{format_money(m.get('avg_cost'), acct)} ({acct})"
            )
        cost = holding.get("avg_cost", 0)
        price = holding.get("current_price") or market_data.get("current_price") or 0
        lines.append("\n### Pie totals")
        lines.append(f"Invested (cost basis, {acct}): {format_money(cost, acct)}")
        lines.append(f"Current value ({acct}): {format_money(price, acct)}")
        if cost:
            pnl = (price - cost)
            lines.append(
                f"Unrealised P&L ({acct}): {format_money(pnl, acct, signed=True)} "
                f"({pnl / cost * 100:+.1f}%)"
            )
        lines.append("\nAnalyse this pie as one portfolio unit and return your JSON recommendation.")
        return "\n".join(lines)

    shares = holding.get("shares", 0)
    cost   = holding.get("avg_cost", 0)
    price  = market_data.get("current_price") or 0
    t212_px = market_data.get("t212_current_price")
    pnl_pct = ((price - cost) / cost * 100) if cost else 0
    pnl_abs = (price - cost) * shares if (price and cost) else 0
    acct = normalize_currency(holding.get("account_currency"))
    inst = holding.get("instrument_currency")
    quote_ccy = market_data.get("quote_currency")

    company = holding.get("instrument_name") or ""
    market_ticker = holding.get("market_ticker") or market_data.get("ticker") or ticker
    title = company if company else ticker
    lines = [f"## Position: {title}"]
    if company and company.upper() != ticker.upper():
        lines.append(f"Trading 212 instrument code: {ticker}")
    if market_ticker and market_ticker.upper() != ticker.upper():
        lines.append(
            f"Market data symbol (fundamentals/news only): {market_ticker}"
        )
    if market_data.get("price_source_note"):
        lines.append(f"IMPORTANT: {market_data['price_source_note']}")
    lines.append(
        "\nDo not classify this as a worthless or $0 security if Trading 212 "
        "shows a positive live price and the position has meaningful value."
    )

    if handoff_note:
        lines.append("\n### Memory from previous run")
        lines.append(f"Thesis: {handoff_note.get('thesis_summary', 'N/A')}")
        watch = handoff_note.get("watch_items", [])
        if watch:
            lines.append("Watch items: " + " | ".join(watch))
        flags = handoff_note.get("trend_flags", [])
        if flags:
            lines.append("Recent trend changes: " + ", ".join(flags))
        risks = handoff_note.get("ongoing_risks", [])
        if risks:
            lines.append("Ongoing risks: " + " | ".join(risks))
        cats = handoff_note.get("ongoing_catalysts", [])
        if cats:
            lines.append("Ongoing catalysts: " + " | ".join(cats))

    lines.append("\n### Portfolio position (Trading 212 — authoritative)")
    lines.append(f"Account currency: {acct} (all T212 wallet figures below are in {acct})")
    if inst and inst != acct:
        from .currency import instrument_currency_label

        lines.append(f"Instrument listing currency: {instrument_currency_label(inst)}")
    if quote_ccy and quote_ccy != acct:
        lines.append(f"yfinance quote currency: {quote_ccy} (fundamentals/52w — not T212 wallet)")
    if holding.get("currency_note"):
        lines.append(holding["currency_note"])
    lines.append(f"Shares held: {shares}")
    lines.append(f"Average cost per share: {format_money(cost, acct)} ({acct})")
    if t212_px:
        lines.append(f"Current price per share (T212): {format_money(float(t212_px), acct)} ({acct})")
    else:
        lines.append(f"Current price per share: {format_money(price, acct)} ({acct})")
    if holding.get("position_value") is not None:
        lines.append(
            f"Position value (T212): {format_money(float(holding['position_value']), acct)} ({acct})"
        )
    lines.append(
        f"Unrealised P&L ({acct}): {format_money(pnl_abs, acct, signed=True)} ({pnl_pct:+.1f}%)"
    )

    lines.append("\n### Market data")
    q_label = f" ({quote_ccy})" if quote_ccy else ""
    if market_data.get("week_52_high"):
        lines.append(
            f"52w range{q_label}: {market_data['week_52_low']:.2f} – {market_data['week_52_high']:.2f}"
        )
    if market_data.get("recent_closes"):
        lines.append("Recent closes (10d): " + ", ".join(str(p) for p in market_data["recent_closes"]))
    if market_data.get("market_cap"):
        mc = market_data["market_cap"]
        lines.append(f"Market cap: {mc/1e9:.1f}B" if mc > 1e9 else f"Market cap: {mc/1e6:.0f}M")

    lines.append("\n### Fundamentals")
    if market_data.get("pe_ratio"):
        lines.append(f"P/E ratio: {market_data['pe_ratio']}")
    if market_data.get("eps_ttm"):
        lines.append(f"EPS (TTM): {market_data['eps_ttm']}")
    if market_data.get("eps_growth_pct") is not None:
        lines.append(f"EPS/revenue growth: {market_data['eps_growth_pct']:+.1f}%")
    if market_data.get("next_earnings"):
        lines.append(f"Next earnings date: {market_data['next_earnings']}")

    if market_data.get("analyst_target_mean"):
        lines.append("\n### Analyst consensus")
        tgt_ccy = quote_ccy or "listing currency"
        lines.append(
            f"Mean target ({tgt_ccy}): {market_data['analyst_target_mean']}  "
            f"Range: {market_data.get('analyst_target_low', '?')} – {market_data.get('analyst_target_high', '?')}  "
            f"({market_data.get('analyst_count', '?')} analysts)"
        )
        if market_data.get("analyst_consensus"):
            lines.append(f"Consensus: {market_data['analyst_consensus'].upper()}")

    headlines = market_data.get("recent_headlines", [])
    if headlines:
        lines.append("\n### Recent news headlines")
        for h in headlines:
            lines.append(f"- {h}")

    lines.append("\nAnalyse this position and return your JSON recommendation.")
    return "\n".join(lines)
