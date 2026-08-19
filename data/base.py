"""
DataFeed abstraction + the market-data value types.

A DataFeed is READ-ONLY by construction, it has no order-placing methods. The
IBKR adapter is a DataFeed only, which is how "IBKR never trades" is enforced
structurally rather than by a runtime check.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class OptionQuote:
    """One call/put quote at a point in time."""
    occ_symbol: str          # OCC-style contract symbol, e.g. SCHD260717C00029000
    underlying: str
    expiry: date
    strike: float
    right: str               # "C" or "P"
    bid: float
    ask: float
    underlying_price: float
    as_of: datetime
    delta: float | None = None
    iv: float | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float:
        """Bid/ask spread as a percentage of mid. inf if mid <= 0."""
        m = self.mid
        return float("inf") if m <= 0 else 100.0 * self.spread / m

    def dte(self, asof: date) -> int:
        return (self.expiry - asof).days


@dataclass(frozen=True)
class Dividend:
    symbol: str
    ex_date: date
    amount_per_share: float


class DataFeed(abc.ABC):
    """Read-only market data source. No order methods, cannot trade."""

    name: str = "base"

    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def disconnect(self) -> None: ...

    @abc.abstractmethod
    def is_available(self) -> bool:
        """True if this feed is connected and answering. Drives fallback."""

    @abc.abstractmethod
    def get_spot(self, symbol: str) -> float: ...

    @abc.abstractmethod
    def get_call_chain(
        self, underlying: str, dte_min: int, dte_max: int, asof: date | None = None
    ) -> list[OptionQuote]:
        """All CALL quotes whose DTE falls in [dte_min, dte_max]."""

    def get_dividends(self, symbol: str, start: date, end: date) -> list[Dividend]:
        """Optional. Default: none known (settlement engine then models none)."""
        return []

    def get_option_mid(self, underlying: str, expiry: date, strike: float,
                       right: str = "C") -> float | None:
        """Current mid for a specific contract (for marking/closing a short call).
        Default None -> caller falls back to intrinsic."""
        return None

    # context-manager sugar
    def __enter__(self) -> "DataFeed":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()
