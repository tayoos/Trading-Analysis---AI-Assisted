import logging
import os
import time
from typing import Optional

import requests

from .base import DataSource, Position, Trade

logger = logging.getLogger(__name__)

_BASE_URL = "https://live.trading212.com"
_TIMEOUT = 15
# History endpoints allow 6 requests/min — pace to ~10s between pages
_HISTORY_PAGE_DELAY = 10.0

# Fill types that represent actual trades (not corporate actions)
_TRADE_FILL_TYPES = {"TRADE", "FOP", "FOP_CORRECTION"}


class T212TransactionsScopeError(Exception):
    """Raised when the API key lacks the history:transactions scope (HTTP 403)."""


class T212DataSource(DataSource):
    """Trading 212 REST API v0 client — HTTP Basic Auth (api_key:api_secret)."""

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
        logger.info("T212 ▶ fetching current positions")
        data = self._get("/api/v0/equity/positions")
        positions = []
        for item in data:
            # Per API spec, ticker lives in item["instrument"]["ticker"].
            # T212 also returns it as a top-level convenience field on some responses,
            # so try both to be safe.
            raw_ticker = (
                item.get("ticker")
                or item.get("instrument", {}).get("ticker", "")
            )
            ticker = self._normalise_ticker(raw_ticker)
            if not ticker:
                logger.warning("T212 position item has no ticker — skipping: %s", list(item.keys()))
                continue
            wallet = item.get("walletImpact", {})
            qty    = float(item.get("quantity", 0))
            # totalCost is in account currency; derive avg cost per share from it
            # when available, else fall back to averagePricePaid (instrument currency)
            total_cost = wallet.get("totalCost")
            if total_cost is not None and qty:
                avg_cost = float(total_cost) / qty
            else:
                avg_cost = float(item.get("averagePricePaid", 0))
            instrument = item.get("instrument") or {}
            raw_price = item.get("currentPrice")
            current_price = float(raw_price) if raw_price is not None else None
            inst_name = instrument.get("name") or None
            logger.debug(
                "T212   position %s: qty=%.4f avg_cost=%.4f currency=%s",
                ticker, qty, avg_cost, wallet.get("currency", "?"),
            )
            positions.append(Position(
                ticker=ticker,
                shares=qty,
                avg_cost=avg_cost,
                current_price=current_price,
                instrument_name=inst_name,
            ))
        logger.info("T212 ✓ %d positions", len(positions))
        return positions

    def get_orders(self, since: Optional[str] = None) -> list[Trade]:
        logger.info("T212 ▶ fetching order history%s", f" since {since}" if since else " (full history)")
        trades: list[Trade] = []
        next_path: Optional[str] = "/api/v0/equity/history/orders?limit=50"
        page = 0

        while next_path:
            page += 1
            logger.info("T212   orders page %d (%d trades collected so far)", page, len(trades))
            data = self._get(next_path)

            items     = data.get("items", [])
            next_path = data.get("nextPagePath")  # use full path directly per API spec

            if page == 1 and items:
                sample_statuses = list({i.get("order", {}).get("status") for i in items[:10]})
                logger.info("T212   sample order statuses page 1: %s", sample_statuses)

            filled = sum(1 for i in items if i.get("order", {}).get("status") == "FILLED")
            logger.info("T212   page %d: %d items, %d filled, has_next=%s",
                        page, len(items), filled, bool(next_path))

            for item in items:
                order = item.get("order", {})
                fill  = item.get("fill",  {})

                if order.get("status") != "FILLED":
                    continue

                filled_at = fill.get("filledAt") or order.get("createdAt", "")
                if since and filled_at <= since:
                    logger.info("T212   reached already-synced cutoff (%s) — stopping early", since)
                    return trades

                fill_type = fill.get("type", "TRADE")
                if fill_type not in _TRADE_FILL_TYPES:
                    logger.info(
                        "T212   skipping non-trade fill: ticker=%s type=%s filledAt=%s",
                        order.get("ticker", "?"), fill_type, filled_at,
                    )
                    continue

                trade = self._parse_order(order, fill)
                if trade:
                    trades.append(trade)

            if not items:
                break
            if next_path:
                logger.info("T212   pacing %.0fs before next orders page (rate limit: 6 req/min)", _HISTORY_PAGE_DELAY)
                time.sleep(_HISTORY_PAGE_DELAY)

        logger.info("T212 ✓ fetched %d trades across %d page(s)", len(trades), page)
        return trades

    def get_dividends(self, since: Optional[str] = None) -> list[dict]:
        logger.info("T212 ▶ fetching dividend history%s", f" since {since}" if since else " (full history)")
        results: list[dict] = []
        next_path: Optional[str] = "/api/v0/equity/history/dividends?limit=50"
        page = 0

        while next_path:
            page += 1
            data = self._get(next_path)

            items     = data.get("items", [])
            next_path = data.get("nextPagePath")
            logger.info("T212   dividends page %d: %d items", page, len(items))

            for item in items:
                # paidOn is always present per the API spec
                paid_at = item.get("paidOn", "")
                if since and paid_at <= since:
                    logger.info("T212   reached already-synced dividend cutoff — stopping early")
                    return results
                ticker = self._normalise_ticker(item.get("ticker", ""))
                if not ticker:
                    continue
                # amount is in account primary currency per spec
                amount = float(item.get("amount", 0))
                logger.debug(
                    "T212   dividend %s: amount=%.4f %s qty=%.4f type=%s",
                    ticker, amount, item.get("currency", "?"),
                    float(item.get("quantity", 0)), item.get("type", "?"),
                )
                results.append({
                    "t212_ref":   str(item.get("reference", "")),
                    "ticker":     ticker,
                    "amount":     amount,
                    "shares_held": float(item.get("quantity", 0)) or None,
                    "paid_at":    paid_at,
                })

            if not items:
                break
            if next_path:
                logger.info("T212   pacing %.0fs before next dividends page (rate limit: 6 req/min)", _HISTORY_PAGE_DELAY)
                time.sleep(_HISTORY_PAGE_DELAY)

        logger.info("T212 ✓ fetched %d dividends across %d page(s)", len(results), page)
        return results

    def get_account_summary(self) -> dict:
        """Cash and investment breakdown from T212 account summary."""
        logger.info("T212 ▶ fetching account summary")
        data = self._get("/api/v0/equity/account/summary")
        investments = data.get("investments") or {}
        cash = data.get("cash") or {}
        return {
            "currency":           data.get("currency"),
            "total_value":        float(data.get("totalValue", 0)),
            "investments_cost":   float(investments.get("totalCost", 0)),
            "investments_value":  float(investments.get("currentValue", 0)),
            "unrealized_pnl":     float(investments.get("unrealizedProfitLoss", 0)),
            "realized_pnl":       float(investments.get("realizedProfitLoss", 0)),
            "cash_available":     float(cash.get("availableToTrade", 0)),
            "cash_in_pies":       float(cash.get("inPies", 0)),
        }

    def get_transactions(self) -> list[dict]:
        """All account cash movements (deposits, withdrawals, fees, transfers)."""
        logger.info("T212 ▶ fetching transaction history")
        results: list[dict] = []
        next_path: Optional[str] = "/api/v0/equity/history/transactions?limit=50"
        page = 0

        try:
            while next_path:
                page += 1
                data = self._get_transactions_page(next_path)
                items = data.get("items", [])
                next_path = data.get("nextPagePath")
                for item in items:
                    results.append({
                        "amount":   float(item.get("amount", 0)),
                        "currency": item.get("currency"),
                        "type":     item.get("type", ""),
                        "date":     item.get("dateTime", ""),
                        "ref":      item.get("reference", ""),
                    })
                if next_path:
                    time.sleep(_HISTORY_PAGE_DELAY)
                elif not items:
                    break
        except T212TransactionsScopeError:
            raise
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 403:
                logger.error(
                    "T212 transactions blocked — enable API scope "
                    "'history:transactions' when creating the key"
                )
                raise T212TransactionsScopeError(
                    "API key missing history:transactions scope"
                ) from exc
            raise

        logger.info("T212 ✓ fetched %d transactions across %d page(s)", len(results), page)
        return results

    def _get_transactions_page(self, path: str) -> dict:
        """GET one transactions page; map 403 to T212TransactionsScopeError."""
        url = f"{_BASE_URL}{path}"
        for attempt in range(10):
            try:
                resp = self._session.get(url, timeout=_TIMEOUT)
                if resp.status_code == 403:
                    body = resp.text[:200] if resp.text else ""
                    logger.error(
                        "T212 HTTP 403 for %s — enable history:transactions on your API key: %s",
                        path,
                        body,
                    )
                    raise T212TransactionsScopeError(
                        "API key missing history:transactions scope"
                    )
                if resp.status_code == 429:
                    reset_ts = resp.headers.get("x-ratelimit-reset")
                    if reset_ts:
                        raw_wait = int(reset_ts) - int(time.time()) + 1
                        wait = max(min(raw_wait, 60), 10)
                    else:
                        wait = 15
                    logger.warning(
                        "T212 rate limited on %s — waiting %ds (attempt %d/10)",
                        path, wait, attempt + 1,
                    )
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except T212TransactionsScopeError:
                raise
            except requests.HTTPError:
                raise
            except requests.RequestException as exc:
                logger.error("T212 request failed for %s: %s", path, exc)
                raise
        raise RuntimeError(f"T212 rate limit retries exhausted after 10 attempts for {path}")

    def get_pies(self) -> list[dict]:
        """List pies (summary only — use get_pie for holdings)."""
        logger.info("T212 ▶ fetching pies list")
        data = self._get("/api/v0/equity/pies")
        if not isinstance(data, list):
            return []
        logger.info("T212 ✓ %d pies", len(data))
        return data

    def get_pie(self, pie_id: int) -> dict:
        logger.info("T212 ▶ fetching pie %s", pie_id)
        return self._get(f"/api/v0/equity/pies/{pie_id}")

    # ── Private helpers ────────────────────────────────────────────────────────

    def _get(self, path: str, params: Optional[dict] = None) -> dict | list:
        url = f"{_BASE_URL}{path}"
        for attempt in range(10):
            try:
                resp = self._session.get(url, params=params, timeout=_TIMEOUT)
                if resp.status_code == 429:
                    reset_ts = resp.headers.get("x-ratelimit-reset")
                    if reset_ts:
                        raw_wait = int(reset_ts) - int(time.time()) + 1
                        wait = max(min(raw_wait, 60), 10)
                    else:
                        wait = 15
                    logger.warning("T212 rate limited on %s — waiting %ds (attempt %d/10)", path, wait, attempt + 1)
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
        raise RuntimeError(f"T212 rate limit retries exhausted after 10 attempts for {path}")

    def _parse_order(self, order: dict, fill: dict) -> Optional[Trade]:
        ticker = self._normalise_ticker(order.get("ticker", ""))
        if not ticker:
            return None

        action = order.get("side", "").upper()  # BUY | SELL per spec
        if action not in ("BUY", "SELL"):
            qty_raw = float(order.get("filledQuantity", 0))
            action  = "BUY" if qty_raw >= 0 else "SELL"

        qty       = abs(float(order.get("filledQuantity", 0)))
        price     = float(fill.get("price", 0))
        traded_at = fill.get("filledAt") or order.get("createdAt", "")

        # walletImpact.netValue is in account currency — more accurate than qty*price
        # (which is in instrument currency and ignores FX)
        wallet      = fill.get("walletImpact", {})
        net_value   = wallet.get("netValue")
        total_value = abs(float(net_value)) if net_value is not None else qty * price

        logger.debug(
            "T212   trade %s %s: qty=%.4f price=%.4f total=%.4f %s",
            action, ticker, qty, price, total_value, wallet.get("currency", "?"),
        )
        return Trade(
            order_id=str(order.get("id", "")),
            ticker=ticker,
            action=action,
            quantity=qty,
            price=price,
            total_value=total_value,
            traded_at=traded_at,
        )

    @staticmethod
    def _normalise_ticker(raw: str) -> str:
        """Strip T212 exchange suffixes: AAPL_US_EQ → AAPL"""
        if not raw:
            return ""
        return raw.split("_")[0].upper()
