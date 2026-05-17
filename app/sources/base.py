from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class Trade:
    order_id: str
    ticker: str
    action: str          # BUY | SELL
    quantity: float
    price: float
    total_value: float
    traded_at: str       # ISO 8601


@dataclass
class Position:
    ticker: str
    shares: float
    avg_cost: float      # per share, account currency (from wallet totalCost / qty)
    current_price: Optional[float] = None   # per share, account currency
    instrument_name: Optional[str] = None
    position_value: Optional[float] = None  # wallet currentValue (account currency)
    unrealized_pnl: Optional[float] = None
    t212_raw_ticker: Optional[str] = None   # e.g. SOAC_US_EQ
    isin: Optional[str] = None
    instrument_currency: Optional[str] = None
    market_ticker: Optional[str] = None     # yfinance symbol for quotes/analysis


class DataSource(ABC):
    """
    Abstract interface for a portfolio data provider.

    Implement this to add new sources (Trading 212, crypto exchange,
    another broker) without touching the analysis pipeline.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable identifier, e.g. 'trading212' or 'coinbase'."""
        ...

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Return all currently open positions."""
        ...

    @abstractmethod
    def get_orders(self, since: Optional[str] = None) -> list[Trade]:
        """
        Return executed orders.
        `since` is an ISO 8601 timestamp; if None, return full history.
        """
        ...

    def is_available(self) -> bool:
        """Override to add a connectivity / credential check."""
        return True
