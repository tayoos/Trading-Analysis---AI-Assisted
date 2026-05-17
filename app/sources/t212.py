import logging
import os
import time
from typing import Optional

import requests

from .base import DataSource, Position, Trade

logger = logging.getLogger(__name__)

_BASE_URL = "https://live.trading212.com"
_TIMEOUT = 15


class T212DataSource(DataSource):
    """Trading 212 REST API v0 client."""

    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None):
        self._api_key    = api_key    or os.getenv("TRADING212_API_KEY", "")
        self._api_secret = api_secret or os.getenv("TRADING212_API_SECRET", "")
        self._session = requests.Session()
        if self._api_key and self._api_secret:
            self._session.auth = (self._api_key, self._api_secret)

    @property
    def name(self) -> str:
        return "trading212"

    def is_available(self) -> bool:
        return bool(self._api_key and self._api_secret)

    # ── Public interface ───────────────────────────────────────────────────────

    def get_positions(self) -> list[Position]:
        data = self._get("/api/v0/equity/positions")
        positions = []
        for item in data:
            ticker = self._normalise_ticker(item.get("ticker", ""))
            if not ticker:
                continue
            positions.append(Position(
                ticker=ticker,
                shares=float(item.get("quantity", 0)),
                avg_cost=float(item.get("averagePrice", 0)),
            ))
        return positions

    def get_orders(self, since: Optional[str] = None) -> list[Trade]:
        params: dict = {"limit": 50}
        trades: list[Trade] = []
        cursor = None

        while True:
            if cursor:
                params["cursor"] = cursor
            data = self._get("/api/v0/equity/history/orders", params=params)

            items = data.get("items", [])
            next_cursor = data.get("nextPagePath")

            for item in items:
                if item.get("status") != "FILLED":
                    continue
                if since and item.get("dateModified", "") <= since:
                    return trades
                trade = self._parse_order(item)
                if trade:
                    trades.append(trade)

            if not next_cursor or not items:
                break
            import urllib.parse as up
            qs = up.urlparse(next_cursor).query
            cursor = up.parse_qs(qs).get("cursor", [None])[0]
            time.sleep(0.25)

        return trades

    def get_dividends(self, since: Optional[str] = None) -> list[dict]:
        """
        Fetch dividend payment history from T212.
        Returns plain dicts ready to pass to Database.save_dividends().
        """
        params: dict = {"limit": 50}
        results: list[dict] = []
        cursor = None

        while True:
            if cursor:
                params["cursor"] = cursor
            data = self._get("/api/v0/equity/history/dividends", params=params)

            items = data.get("items", [])
            next_cursor = data.get("nextPagePath")

            for item in items:
                paid_at = item.get("paidOn", item.get("dateModified", ""))
                if since and paid_at <= since:
                    return results
                ticker = self._normalise_ticker(item.get("ticker", ""))
                if not ticker:
                    continue
                results.append({
                    "t212_ref": str(item.get("reference", item.get("id", ""))),
                    "ticker": ticker,
                    "amount": float(item.get("amount", item.get("grossAmount", 0))),
                    "shares_held": float(item.get("quantity", 0)) or None,
                    "paid_at": paid_at,
                })

            if not next_cursor or not items:
                break
            import urllib.parse as up
            qs = up.urlparse(next_cursor).query
            cursor = up.parse_qs(qs).get("cursor", [None])[0]
            time.sleep(0.25)

        return results

    # ── Private helpers ────────────────────────────────────────────────────────

    def _get(self, path: str, params: Optional[dict] = None) -> dict | list:
        url = f"{_BASE_URL}{path}"
        for attempt in range(4):
            try:
                resp = self._session.get(url, params=params, timeout=_TIMEOUT)
                if resp.status_code == 429:
                    reset_ts = resp.headers.get("x-ratelimit-reset")
                    wait = max(int(reset_ts) - int(time.time()) + 1, 10) if reset_ts else 60
                    logger.warning("T212 rate limited on %s — waiting %ds (attempt %d/4)", path, wait, attempt + 1)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as exc:
                logger.error("T212 HTTP %s for %s: %s", exc.response.status_code, path, exc)
                raise
            except requests.RequestException as exc:
                logger.error("T212 request failed for %s: %s", path, exc)
                raise
        raise RuntimeError(f"T212 rate limit retries exhausted for {path}")

    def _parse_order(self, item: dict) -> Optional[Trade]:
        ticker = self._normalise_ticker(item.get("ticker", ""))
        if not ticker:
            return None
        action = "BUY" if float(item.get("filledQuantity", 0)) > 0 else "SELL"
        qty = abs(float(item.get("filledQuantity", 0)))
        price = float(item.get("fillPrice", 0))
        return Trade(
            order_id=str(item.get("id", "")),
            ticker=ticker,
            action=action,
            quantity=qty,
            price=price,
            total_value=qty * price,
            traded_at=item.get("dateModified", ""),
        )

    @staticmethod
    def _normalise_ticker(raw: str) -> str:
        """Strip T212 exchange suffixes like '_US_EQ' → plain ticker."""
        if not raw:
            return ""
        # T212 format: AAPL_US_EQ → AAPL
        return raw.split("_")[0].upper()
