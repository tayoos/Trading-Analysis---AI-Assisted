"""
Claude-powered stock analysis engine with handoff-note memory system.
"""
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

import anthropic
import yfinance as yf

from .database import Database

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-20250514"

_SYSTEM_PROMPT = """You are an expert equity analyst AI assistant. Your job is to analyse individual stock positions in a user's portfolio and return a structured JSON recommendation.

Output ONLY a valid JSON object — no markdown fences, no preamble, no explanation outside the JSON.

JSON schema (all fields required):
{
  "recommendation": "BUY" | "HOLD" | "SELL" | "WATCH",
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
  }
}

Guidelines:
- Base targets on realistic near-term fundamentals, not wishful thinking.
- WATCH = interesting but not a current position worth acting on.
- trend_flags: note if your recommendation differs from the previous run (e.g. "HOLD→BUY").
- keep watch_items specific and actionable (earnings date, product launch, macro event).
- All prices in the position's native currency."""


class StockAnalyzer:
    def __init__(self, db: Database, api_key: Optional[str] = None):
        self.db = db
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self._lock = threading.Lock()
        self._run_id: Optional[int] = None
        self._status: str = "idle"   # idle | running | done | error
        self._progress: list[str] = []

    # ── Status (thread-safe) ───────────────────────────────────────────────────

    @property
    def status(self) -> dict:
        with self._lock:
            return {
                "status": self._status,
                "run_id": self._run_id,
                "progress": list(self._progress),
            }

    def _set_status(self, status: str) -> None:
        with self._lock:
            self._status = status

    def _log(self, msg: str) -> None:
        logger.info(msg)
        with self._lock:
            self._progress.append(msg)
        if self._run_id:
            self.db.append_log(self._run_id, msg)

    # ── Main entry point ───────────────────────────────────────────────────────

    def run_analysis(self, holdings: list[dict]) -> int:
        """
        Analyse all holdings. Returns the run_id.
        Designed to be called in a background thread.
        """
        if self._status == "running":
            raise RuntimeError("Analysis already in progress")

        run_id = self.db.create_run()
        with self._lock:
            self._run_id = run_id
            self._status = "running"
            self._progress = []

        self._log(f"[{_ts()}] Starting analysis for {len(holdings)} holdings")

        try:
            for holding in holdings:
                ticker = holding["ticker"]
                try:
                    self._log(f"[{_ts()}] Analysing {ticker}…")
                    result = self._analyse_ticker(holding)
                    self.db.save_analysis(run_id, ticker, result)
                    if "handoff_note" in result:
                        self.db.save_handoff_note(ticker, result["handoff_note"])
                    self._log(f"[{_ts()}] {ticker} → {result.get('recommendation', '?')}")
                except Exception as exc:
                    self._log(f"[{_ts()}] ERROR {ticker}: {exc}")
                    logger.exception("Analysis failed for %s", ticker)

            self.db.finish_run(run_id, "done", len(holdings))
            self._log(f"[{_ts()}] Analysis complete.")
            self._set_status("done")

        except Exception as exc:
            self.db.finish_run(run_id, "error", 0)
            self._log(f"[{_ts()}] Fatal error: {exc}")
            self._set_status("error")
            logger.exception("Fatal error during analysis run")

        return run_id

    # ── Per-ticker analysis ────────────────────────────────────────────────────

    def _analyse_ticker(self, holding: dict) -> dict:
        ticker = holding["ticker"]
        market_data = _fetch_market_data(ticker)
        handoff_note = self.db.get_handoff_note(ticker)
        prompt = _build_prompt(holding, market_data, handoff_note)

        response = self._client.messages.create(
            model=_MODEL,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},  # cache the system prompt
                }
            ],
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        result = json.loads(raw)

        # Augment with live position data for storage
        result["current_price"] = market_data.get("current_price")
        result["cost_basis"] = holding.get("avg_cost")
        result["shares"] = holding.get("shares")

        # Populate trend_flags by comparing to previous recommendation
        prev = self.db.get_ticker_history(ticker, limit=1)
        if prev:
            prev_rec = prev[0].get("recommendation")
            new_rec = result.get("recommendation")
            if prev_rec and new_rec and prev_rec != new_rec:
                flag = f"{prev_rec}→{new_rec}"
                if "handoff_note" in result:
                    result["handoff_note"].setdefault("trend_flags", []).append(flag)

        return result

    # ── Background runner ──────────────────────────────────────────────────────

    def run_in_background(self, holdings: list[dict]) -> None:
        """Start analysis in a daemon thread."""
        t = threading.Thread(
            target=self.run_analysis,
            args=(holdings,),
            daemon=True,
            name="analyzer",
        )
        t.start()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _fetch_market_data(ticker: str) -> dict:
    try:
        info = yf.Ticker(ticker)
        fast = info.fast_info
        hist = info.history(period="5d")
        news = info.news or []

        prices = hist["Close"].tolist() if not hist.empty else []
        current_price = float(fast.last_price) if fast.last_price else (prices[-1] if prices else None)

        headlines = [
            n.get("content", {}).get("title", n.get("title", ""))
            for n in news[:5]
        ]

        return {
            "ticker": ticker,
            "current_price": current_price,
            "week_52_high": float(fast.fifty_two_week_high) if fast.fifty_two_week_high else None,
            "week_52_low": float(fast.fifty_two_week_low) if fast.fifty_two_week_low else None,
            "market_cap": float(fast.market_cap) if fast.market_cap else None,
            "recent_closes": [round(p, 4) for p in prices[-5:]],
            "recent_headlines": headlines,
        }
    except Exception as exc:
        logger.warning("yfinance failed for %s: %s", ticker, exc)
        return {"ticker": ticker, "current_price": None}


def _build_prompt(holding: dict, market_data: dict, handoff_note: Optional[dict]) -> str:
    ticker = holding["ticker"]
    shares = holding.get("shares", 0)
    cost = holding.get("avg_cost", 0)
    price = market_data.get("current_price") or 0
    pnl_pct = ((price - cost) / cost * 100) if cost else 0
    pnl_abs = (price - cost) * shares if (price and cost) else 0

    lines = [f"## Position: {ticker}"]

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

    lines.append("\n### Portfolio position")
    lines.append(f"Shares held: {shares}")
    lines.append(f"Average cost: {cost:.4f}")
    lines.append(f"Current price: {price:.4f}")
    lines.append(f"Unrealised P&L: {pnl_abs:+.2f} ({pnl_pct:+.1f}%)")

    lines.append("\n### Market data")
    if market_data.get("week_52_high"):
        lines.append(f"52w range: {market_data['week_52_low']:.2f} – {market_data['week_52_high']:.2f}")
    if market_data.get("recent_closes"):
        lines.append("Recent closes (5d): " + ", ".join(str(p) for p in market_data["recent_closes"]))
    if market_data.get("market_cap"):
        mc = market_data["market_cap"]
        lines.append(f"Market cap: {mc/1e9:.1f}B" if mc > 1e9 else f"Market cap: {mc/1e6:.0f}M")

    headlines = market_data.get("recent_headlines", [])
    if headlines:
        lines.append("\n### Recent news headlines")
        for h in headlines:
            lines.append(f"- {h}")

    lines.append("\nAnalyse this position and return your JSON recommendation.")
    return "\n".join(lines)
