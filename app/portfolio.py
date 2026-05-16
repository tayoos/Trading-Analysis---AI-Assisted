"""
Portfolio management: cost-basis calculations and Excel fallback reader.
"""
import logging
import os
from typing import Optional

import pandas as pd

from .database import Database
from .sources.base import Position, Trade

logger = logging.getLogger(__name__)


class PortfolioManager:
    def __init__(self, db: Database, excel_path: Optional[str] = None,
                 cost_method: str = "AVCO"):
        self.db = db
        self.excel_path = excel_path or os.getenv("EXCEL_PATH", "/data/stocks/stocks.xlsx")
        self.cost_method = cost_method.upper()

    # ── Primary: DB-backed positions ───────────────────────────────────────────

    def get_holdings(self) -> list[dict]:
        """
        Returns positions from DB (populated by T212 sync), falling back to
        Excel if the DB has no positions yet.
        """
        db_positions = self.db.get_positions()
        if db_positions:
            return db_positions
        logger.info("No DB positions found — falling back to Excel")
        return self.read_excel()

    # ── T212 sync: rebuild positions from trade history ────────────────────────

    def apply_trades(self, trades: list[Trade]) -> None:
        """
        Merge new trades into the DB and recalculate AVCO positions.
        Call this after fetching new orders from T212.
        """
        trade_dicts = [
            {
                "order_id": t.order_id,
                "ticker": t.ticker,
                "action": t.action,
                "quantity": t.quantity,
                "price": t.price,
                "total_value": t.total_value,
                "traded_at": t.traded_at,
            }
            for t in trades
        ]
        saved = self.db.save_trades(trade_dicts)
        logger.info("Saved %d new trades to DB", saved)
        self._rebuild_positions()

    def _rebuild_positions(self) -> None:
        """Recalculate all positions from trade history using AVCO."""
        all_trades = self.db.get_trades(limit=100_000)
        # Group by ticker and sort chronologically
        from collections import defaultdict
        by_ticker: dict[str, list[dict]] = defaultdict(list)
        for t in all_trades:
            by_ticker[t["ticker"]].append(t)

        for ticker, trades in by_ticker.items():
            trades.sort(key=lambda x: x["traded_at"])
            shares, total_cost, first_bought = 0.0, 0.0, None

            for t in trades:
                if t["action"] == "BUY":
                    if first_bought is None:
                        first_bought = t["traded_at"]
                    total_cost += t["quantity"] * t["price"]
                    shares += t["quantity"]
                elif t["action"] == "SELL":
                    if shares > 0:
                        avg = total_cost / shares
                        total_cost -= avg * min(t["quantity"], shares)
                        shares = max(0.0, shares - t["quantity"])

            if shares > 0.001:
                avg_cost = total_cost / shares if shares else 0
                self.db.upsert_position(
                    ticker=ticker,
                    shares=round(shares, 6),
                    avg_cost=round(avg_cost, 6),
                    source="trading212",
                    first_bought=first_bought,
                )
            else:
                # Position fully closed — move to owned_history if not already there
                self._record_closed_position(ticker, trades)

    def _record_closed_position(self, ticker: str, trades: list[dict]) -> None:
        existing = self.db.get_owned_history(ticker)
        if existing:
            return
        buys = [t for t in trades if t["action"] == "BUY"]
        sells = [t for t in trades if t["action"] == "SELL"]
        total_bought = sum(t["quantity"] * t["price"] for t in buys)
        total_sold = sum(t["quantity"] * t["price"] for t in sells)
        peak_shares = sum(t["quantity"] for t in buys) - sum(t["quantity"] for t in sells)
        self.db.save_owned_history({
            "ticker": ticker,
            "shares_peak": peak_shares,
            "avg_cost": total_bought / sum(t["quantity"] for t in buys) if buys else 0,
            "first_bought": buys[0]["traded_at"] if buys else None,
            "fully_sold_at": sells[-1]["traded_at"] if sells else None,
            "realised_pl": total_sold - total_bought,
            "notes": None,
        })

    # ── Excel fallback ─────────────────────────────────────────────────────────

    def read_excel(self) -> list[dict]:
        """Read portfolio from Excel (Ticker / Shares / Buy Price columns)."""
        if not os.path.exists(self.excel_path):
            logger.warning("Excel file not found at %s", self.excel_path)
            return []
        try:
            df = pd.read_excel(self.excel_path)
            df.columns = [c.strip().lower() for c in df.columns]
            # Accept flexible column names
            col_map = {
                "ticker": ["ticker", "symbol", "stock"],
                "shares": ["shares", "quantity", "qty", "units"],
                "avg_cost": ["buy price", "buyprice", "avg cost", "avgcost",
                             "average price", "avg_cost", "cost"],
            }
            renamed: dict[str, str] = {}
            for canonical, aliases in col_map.items():
                for alias in aliases:
                    if alias in df.columns:
                        renamed[alias] = canonical
                        break
            df = df.rename(columns=renamed)
            required = {"ticker", "shares", "avg_cost"}
            if not required.issubset(df.columns):
                missing = required - set(df.columns)
                logger.error("Excel missing columns: %s", missing)
                return []
            df = df.dropna(subset=["ticker", "shares"])
            df["ticker"] = df["ticker"].astype(str).str.upper().str.strip()
            return [
                {
                    "ticker": row["ticker"],
                    "shares": float(row["shares"]),
                    "avg_cost": float(row.get("avg_cost", 0)),
                    "source": "excel",
                    "last_updated": None,
                }
                for _, row in df.iterrows()
                if row["ticker"]
            ]
        except Exception as exc:
            logger.error("Failed to read Excel: %s", exc)
            return []
