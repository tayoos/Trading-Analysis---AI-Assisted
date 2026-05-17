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

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_prices(self) -> dict:
        """Return the cached price snapshot."""
        with self._lock:
            return {
                "prices":     dict(self._prices),
                "errors":     dict(self._errors),
                "fetched_at": self._fetched_at,
                "stale":      self._is_stale(),
            }

    def get_price(self, ticker: str) -> Optional[float]:
        with self._lock:
            return self._prices.get(ticker)

    def refresh(self, tickers: list[str]) -> dict:
        """
        Fetch live prices for the given tickers synchronously.
        Returns the new price snapshot.
        """
        if not tickers:
            return self.get_prices()

        with self._lock:
            if self._refreshing:
                logger.info("Price refresh already in progress — returning cached data")
                return {"prices": dict(self._prices), "errors": dict(self._errors),
                        "fetched_at": self._fetched_at, "stale": self._is_stale()}
            self._refreshing = True

        try:
            prices, errors = _fetch_prices(tickers)
        finally:
            with self._lock:
                self._refreshing = False

        with self._lock:
            self._prices     = prices
            self._errors     = errors
            self._fetched_at = time.time()

        logger.info(
            "Live prices refreshed: %d fetched, %d failed — %s",
            len(prices), len(errors),
            ", ".join(f"{t}={p:.2f}" for t, p in sorted(prices.items())),
        )
        return self.get_prices()

    def refresh_in_background(self, tickers: list[str]) -> None:
        """Kick off a non-blocking refresh."""
        t = threading.Thread(
            target=self.refresh,
            args=(tickers,),
            daemon=True,
            name="price-refresh",
        )
        t.start()

    def is_stale(self) -> bool:
        with self._lock:
            return self._is_stale()

    # ── Private ────────────────────────────────────────────────────────────────

    def _is_stale(self) -> bool:
        if self._fetched_at is None:
            return True
        return (time.time() - self._fetched_at) > PRICE_TTL_SECONDS


def _fetch_prices(tickers: list[str]) -> tuple[dict[str, float], dict[str, str]]:
    """
    Batch-fetch latest prices from yfinance.
    Returns (prices_dict, errors_dict).
    """
    prices: dict[str, float] = {}
    errors: dict[str, str]   = {}

    # yfinance batch download is faster than one Ticker() per symbol
    try:
        data = yf.download(
            tickers,
            period="2d",        # 2 days so we always get at least one close
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        logger.error("yfinance batch download failed: %s", exc)
        # Fall back to per-ticker fetches
        data = None

    if data is not None and not data.empty:
        close = data["Close"] if "Close" in data.columns else data
        for ticker in tickers:
            try:
                col = close[ticker] if ticker in close.columns else close
                val = col.dropna().iloc[-1]
                prices[ticker] = float(val)
            except Exception:
                errors[ticker] = "no data in batch"

        # Tickers that failed in batch — try individually
        failed = [t for t in tickers if t not in prices]
    else:
        failed = list(tickers)

    for ticker in failed:
        try:
            fast = yf.Ticker(ticker).fast_info
            price = fast.last_price
            if price:
                prices[ticker] = float(price)
            else:
                errors[ticker] = "null last_price"
        except Exception as exc:
            errors[ticker] = str(exc)

    if errors:
        logger.warning("Live price fetch failed for: %s", ", ".join(errors))

    return prices, errors
