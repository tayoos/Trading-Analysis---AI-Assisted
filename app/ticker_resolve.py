"""
Resolve yfinance symbols for T212 positions.

T212 instrument tickers are often legacy SPAC codes (SOAC, QFTA) while the
company has since merged and trades under a new symbol. We pick a market
symbol by validating yfinance quotes against the T212 wallet price.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import yfinance as yf

from .prices import _ticker_candidates

logger = logging.getLogger(__name__)

_SKIP_NAME_WORDS = frozenset({
    "THE", "AND", "INC", "LTD", "PLC", "CORP", "CORPORATION", "COMPANY",
    "CO", "GROUP", "HOLDINGS", "HOLDING", "CLASS", "COMMON", "ORDINARY",
    "SHARES", "STOCK", "LIMITED", "LLC", "SA", "NV", "AG", "SE", "AI",
})

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


def _is_isin(value: str) -> bool:
    v = (value or "").upper().strip()
    return bool(_ISIN_RE.match(v))


def _is_valid_yf_symbol(value: str) -> bool:
    """ISINs are not valid yfinance tickers — never use them as market_ticker."""
    return bool(value) and not _is_isin(value)


def _quote_price(symbol: str) -> Optional[float]:
    for candidate in _ticker_candidates(symbol):
        try:
            fast = yf.Ticker(candidate).fast_info
            if fast.last_price and float(fast.last_price) > 0:
                return float(fast.last_price)
        except Exception:
            continue
    return None


def _price_matches_reference(quote: float, reference: float) -> bool:
    if reference <= 0 or quote <= 0:
        return False
    ratio = quote / reference
    return 0.12 <= ratio <= 8.0


def _symbols_from_name(name: str) -> list[str]:
    """Guess tradable symbols embedded in T212 instrument names."""
    out: list[str] = []
    seen: set[str] = set()

    def add(sym: str) -> None:
        sym = sym.upper().strip()
        if sym and sym not in seen and sym not in _SKIP_NAME_WORDS:
            seen.add(sym)
            out.append(sym)

    # Leading token before comma: "TMC, The Metals Company"
    head = name.split(",")[0].strip()
    for part in re.split(r"[\s\-–—]+", head):
        if re.fullmatch(r"[A-Z][A-Z0-9]{0,4}", part):
            add(part)

    for part in re.findall(r"\b[A-Z][A-Z0-9]{1,4}\b", name):
        if part not in _SKIP_NAME_WORDS:
            add(part)

    return out


def _search_yfinance(name: str, reference_price: Optional[float]) -> Optional[str]:
    try:
        results = yf.Search(name, max_results=8)
        quotes = getattr(results, "quotes", None) or []
    except Exception as exc:
        logger.debug("yfinance search failed for %r: %s", name, exc)
        return None

    for item in quotes:
        sym = (item.get("symbol") or "").strip()
        if not sym:
            continue
        price = _quote_price(sym)
        if price is None:
            continue
        if reference_price is None or _price_matches_reference(price, reference_price):
            return sym.upper()
    return None


def resolve_market_ticker(
    db_ticker: str,
    *,
    isin: Optional[str] = None,
    instrument_name: Optional[str] = None,
    reference_price: Optional[float] = None,
    instrument_currency: Optional[str] = None,
) -> str:
    """
    Return the yfinance symbol to use for quotes and analysis.

    `db_ticker` is the normalized T212 key stored in the DB (e.g. SOAC).
    `reference_price` should be T212 per-share price in account currency.
    """
    db_ticker = (db_ticker or "").upper().strip()
    if not db_ticker:
        return db_ticker

    def try_symbol(sym: str) -> Optional[str]:
        sym = sym.upper().strip()
        if not sym:
            return None
        for candidate in _ticker_candidates(sym):
            price = _quote_price(candidate)
            if price is None:
                continue
            if reference_price is None or _price_matches_reference(price, reference_price):
                return candidate.upper()
        return None

    # 1) T212 short code — only when the public quote matches the wallet price
    hit = try_symbol(db_ticker)
    if hit and _is_valid_yf_symbol(hit):
        return hit.upper()

    # 2) Company name (legacy T212 codes like IVAN → SES for "SES AI")
    if instrument_name:
        for sym in _symbols_from_name(instrument_name):
            hit = try_symbol(sym)
            if hit and _is_valid_yf_symbol(hit):
                logger.info(
                    "Resolved %s → %s from name token %s (%s)",
                    db_ticker, hit, sym, instrument_name,
                )
                return hit.upper()

        found = _search_yfinance(instrument_name, reference_price)
        if found and _is_valid_yf_symbol(found):
            logger.info(
                "Resolved %s → %s via yfinance search (%s)",
                db_ticker, found, instrument_name,
            )
            return found.upper()

    # 3) ISIN — never use the raw ISIN string as a yfinance symbol
    if isin and _is_isin(isin) and instrument_name:
        found = _search_yfinance(f"{instrument_name} {isin}", reference_price)
        if found and _is_valid_yf_symbol(found):
            logger.info("Resolved %s → %s via ISIN-assisted search", db_ticker, found)
            return found.upper()

    logger.warning(
        "Could not resolve market ticker for %s (name=%r, ref_price=%s, ccy=%s) — using T212 code",
        db_ticker, instrument_name, reference_price, instrument_currency,
    )
    return db_ticker


def refresh_market_tickers(db) -> int:
    """Re-resolve market_ticker for all open positions. Returns count updated."""
    updated = 0
    for pos in db.get_positions():
        market = resolve_market_ticker(
            pos["ticker"],
            isin=pos.get("isin"),
            instrument_name=pos.get("instrument_name"),
            reference_price=pos.get("current_price"),
            instrument_currency=pos.get("instrument_currency"),
        )
        if market != (pos.get("market_ticker") or pos["ticker"]):
            db.update_position_market_ticker(pos["ticker"], market)
            updated += 1
    return updated
