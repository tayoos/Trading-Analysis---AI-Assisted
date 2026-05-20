"""
Match yfinance-style tickers to Trading 212 tradable instruments.

Uses GET /api/v0/equity/metadata/instruments (refreshed ~every 10 min on T212).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from .sources.t212 import T212DataSource

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 600  # align with T212 instrument refresh cadence
_STOCK_TYPES = frozenset({"STOCK", "ETF"})

_cache: dict[str, Any] = {
    "fetched_at": 0.0,
    "by_short": {},
}


def _lookup_keys(symbol: str) -> list[str]:
    """Variants to try against T212 short tickers (AAPL_US_EQ → AAPL)."""
    s = symbol.upper().strip()
    if not s:
        return []
    keys = [s]
    if "." in s:
        base, suffix = s.split(".", 1)
        keys.append(base)
        if suffix == "L" and base:
            keys.append(base + "L")
    if "-" in s:
        keys.append(s.replace("-", "."))
        keys.append(s.replace("-", ""))
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _load_index(t212: T212DataSource) -> dict[str, list[dict]]:
    instruments = t212.get_instruments()
    by_short: dict[str, list[dict]] = {}
    for inst in instruments:
        raw = inst.get("ticker") or ""
        short = T212DataSource._normalise_ticker(raw)
        if not short:
            continue
        by_short.setdefault(short, []).append(inst)
    return by_short


def get_instruments_index(t212: T212DataSource, force_refresh: bool = False) -> dict[str, list[dict]]:
    now = time.time()
    if (
        not force_refresh
        and _cache["by_short"]
        and (now - _cache["fetched_at"]) < _CACHE_TTL_SECONDS
    ):
        return _cache["by_short"]

    logger.info("T212 ▶ building instruments index for discovery")
    by_short = _load_index(t212)
    _cache["fetched_at"] = now
    _cache["by_short"] = by_short
    logger.info("T212 ✓ instruments index: %d short symbols", len(by_short))
    return by_short


def resolve_on_t212(
    symbol: str,
    by_short: dict[str, list[dict]],
) -> dict[str, Any]:
    """
    Returns {available, t212_ticker, t212_name, instrument_type}.
    """
    for key in _lookup_keys(symbol):
        matches = by_short.get(key)
        if not matches:
            continue
        # Prefer common stocks/ETFs over warrants etc.
        inst = None
        for candidate in matches:
            if (candidate.get("type") or "").upper() in _STOCK_TYPES:
                inst = candidate
                break
        inst = inst or matches[0]
        return {
            "available": True,
            "t212_ticker": inst.get("ticker"),
            "t212_name": inst.get("shortName") or inst.get("name"),
            "instrument_type": inst.get("type"),
        }
    return {
        "available": False,
        "t212_ticker": None,
        "t212_name": None,
        "instrument_type": None,
    }


def enrich_recommendations_t212(
    recs: list[dict],
    t212: T212DataSource,
) -> tuple[list[dict], int]:
    """
    Set t212_available / t212_ticker on each recommendation dict.
    Returns (recs, count_available).
    """
    if not t212.is_available():
        logger.info("T212 API not configured — skipping instrument availability check")
        for rec in recs:
            rec["t212_available"] = None
            rec["t212_ticker"] = None
        return recs, 0

    try:
        by_short = get_instruments_index(t212)
    except Exception:
        logger.exception("Failed to load T212 instruments — availability unknown")
        for rec in recs:
            rec["t212_available"] = None
            rec["t212_ticker"] = None
        return recs, 0

    available_count = 0
    for rec in recs:
        hit = resolve_on_t212(rec["ticker"], by_short)
        rec["t212_available"] = hit["available"]
        rec["t212_ticker"] = hit["t212_ticker"]
        if hit["available"]:
            available_count += 1
            if hit.get("t212_name") and not rec.get("company_name"):
                rec["company_name"] = hit["t212_name"]

    logger.info(
        "T212 availability: %d/%d discovery ideas tradable on T212",
        available_count,
        len(recs),
    )
    return recs, available_count
