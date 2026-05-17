"""
Live price cache for portfolio positions.

Fetches current prices from yfinance for all open positions and caches them
in memory. Used by the dashboard to show live Portfolio Value and P&L without
needing to run a full Claude analysis.

Cache TTL: 15 minutes (PRICE_TTL_SECONDS). Prices are stale outside market
hours anyway, so a long TTL is fine and avoids hammering yfinance.
"""
import logging
import threading
import time
from typing import Optional

import yfinance as yf

logger = logging.getLogger(__name__)

PRICE_TTL_SECONDS = 900  # 15 minutes


class LivePriceCache:
    def __init__(self) -> None:
        self._lock   = threading.Lock()
        self._prices: dict[str, float]  = {}   # ticker → price
        self._errors: dict[str, str]    = {}   # ticker → error message
        self._fetched_at: Optional[float] = None
        self._refreshing = False
        self._names:  dict[str, str]    = {}   # ticker → company name (cached forever)

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_prices(self) -> dict:
        """Return the cached price snapshot."""
        with self._lock:
            return {
                "prices":     dict(self._prices),
                "names":      dict(self._names),
                "errors":     dict(self._errors),
                "fetched_at": self._fetched_at,
                "stale":      self._is_stale(),
            }

    def get_price(self, ticker: str) -> Optional[float]:
        with self._lock:
            return self._prices.get(ticker)

    def refresh(
        self,
        tickers: list[str],
        market_map: dict[str, str] | None = None,
    ) -> dict:
        """
        Fetch live prices for the given tickers synchronously.
        `market_map` maps DB/T212 keys → yfinance symbols when they differ.
        Returns the new price snapshot.
        """
        if not tickers:
            return self.get_prices()

        market_map = market_map or {}
        entries = [(t, market_map.get(t) or t) for t in tickers]

        with self._lock:
            if self._refreshing:
                logger.info("Price refresh already in progress — returning cached data")
                return {"prices": dict(self._prices), "errors": dict(self._errors),
                        "fetched_at": self._fetched_at, "stale": self._is_stale()}
            self._refreshing = True

        try:
            prices, errors = _fetch_prices(entries)
        finally:
            with self._lock:
                self._refreshing = False

        with self._lock:
            self._prices.update(prices)
            self._errors.update(errors)
            self._fetched_at = time.time()

        logger.info(
            "Live prices refreshed: %d fetched, %d failed — %s",
            len(prices), len(errors),
            ", ".join(f"{t}={p:.2f}" for t, p in sorted(prices.items())),
        )
        with self._lock:
            missing = [t for t in tickers if t not in self._names]
        if missing:
            self._fetch_names(missing, market_map)
        return self.get_prices()

    def refresh_in_background(
        self,
        tickers: list[str],
        market_map: dict[str, str] | None = None,
    ) -> None:
        """Kick off a non-blocking refresh."""
        t = threading.Thread(
            target=self.refresh,
            args=(tickers, market_map),
            daemon=True,
            name="price-refresh",
        )
        t.start()

    def _fetch_names(
        self,
        tickers: list[str],
        market_map: dict[str, str] | None = None,
    ) -> None:
        from concurrent.futures import ThreadPoolExecutor

        market_map = market_map or {}

        def _one(ticker: str) -> tuple[str, str]:
            yf_sym = market_map.get(ticker) or ticker
            for candidate in _ticker_candidates(yf_sym):
                try:
                    info = yf.Ticker(candidate).info
                    name = info.get("shortName") or info.get("longName") or ""
                    if name:
                        return ticker, name
                except Exception:
                    pass
            return ticker, ""

        with ThreadPoolExecutor(max_workers=3) as pool:
            for ticker, name in pool.map(_one, tickers):
                if name:
                    with self._lock:
                        self._names[ticker] = name
        logger.info("Company names cached: %d/%d resolved", sum(1 for t in tickers if t in self._names), len(tickers))

    def is_stale(self) -> bool:
        with self._lock:
            return self._is_stale()

    # ── Private ────────────────────────────────────────────────────────────────

    def _is_stale(self) -> bool:
        if self._fetched_at is None:
            return True
        return (time.time() - self._fetched_at) > PRICE_TTL_SECONDS


_LSE_SUFFIXES = (".L", ".IL")  # London Stock Exchange, Ireland listing


def _ticker_candidates(ticker: str) -> list[str]:
    """Return yfinance ticker variants to try in order (UK listings often need .L)."""
    if "." in ticker:
        return [ticker]
    candidates = [ticker, f"{ticker}.L"]
    # T212 often uses e.g. TM1L for LSE symbol TM1.L
    if len(ticker) > 2 and ticker[-1] == "L" and ticker[-2].isalpha():
        base = ticker[:-1]
        candidates.insert(0, f"{base}.L")
    # dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _fetch_prices(
    entries: list[tuple[str, str]],
) -> tuple[dict[str, float], dict[str, str]]:
    """
    Batch-fetch latest prices from yfinance.
    `entries` is (db_ticker, yfinance_symbol) pairs; prices are keyed by db_ticker.
    Returns (prices_dict, errors_dict).
    """
    prices: dict[str, float] = {}
    errors: dict[str, str]   = {}
    yf_symbols = list(dict.fromkeys(yf for _, yf in entries))

    try:
        data = yf.download(
            yf_symbols,
            period="2d",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        logger.error("yfinance batch download failed: %s", exc)
        data = None

    yf_prices: dict[str, float] = {}
    if data is not None and not data.empty:
        close = data["Close"] if "Close" in data.columns else data
        for yf_sym in yf_symbols:
            try:
                col = close[yf_sym] if yf_sym in close.columns else close
                val = col.dropna().iloc[-1]
                yf_prices[yf_sym] = float(val)
            except Exception:
                pass

    failed_yf = [s for s in yf_symbols if s not in yf_prices]
    for yf_sym in failed_yf:
        for candidate in _ticker_candidates(yf_sym):
            try:
                fast = yf.Ticker(candidate).fast_info
                price = fast.last_price
                if price:
                    yf_prices[yf_sym] = float(price)
                    if candidate != yf_sym:
                        logger.info("Resolved %s via %s", yf_sym, candidate)
                    break
            except Exception:
                pass

    for db_ticker, yf_sym in entries:
        if yf_sym in yf_prices:
            prices[db_ticker] = yf_prices[yf_sym]
        else:
            errors[db_ticker] = "no data from yfinance"

    if errors:
        for ticker, reason in errors.items():
            logger.warning("price_fetch | ticker=%s | %s", ticker, reason)
        logger.warning(
            "Live price fetch failed for %d ticker(s): %s",
            len(errors),
            ", ".join(sorted(errors)),
        )

    return prices, errors
