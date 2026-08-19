"""
Broker abstraction (execution only). Paper-only and limit-only by design:

  * There is NO market-order method anywhere in the interface.
  * connect() must call assert_paper(); a non-paper broker refuses to run.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class AssetClass(str, Enum):
    EQUITY = "equity"
    OPTION = "option"


class OrderStatus(str, Enum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Account:
    cash: float
    equity: float
    buying_power: float


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float                     # negative => short (e.g. a sold call)
    avg_entry_price: float
    asset_class: AssetClass
    market_value: float | None = None


@dataclass(frozen=True)
class Order:
    id: str
    symbol: str
    qty: float
    side: OrderSide
    limit_price: float
    asset_class: AssetClass
    status: OrderStatus
    filled_qty: float = 0.0
    filled_avg_price: float = 0.0
    submitted_at: datetime | None = None

    @property
    def is_filled(self) -> bool:
        return self.status == OrderStatus.FILLED


class Broker(abc.ABC):
    """Paper execution venue. Limit orders only."""

    name: str = "base"

    @abc.abstractmethod
    def connect(self) -> None:
        """Implementations MUST call assert_paper() here."""

    @abc.abstractmethod
    def disconnect(self) -> None: ...

    @abc.abstractmethod
    def is_paper(self) -> bool: ...

    def assert_paper(self) -> None:
        if not self.is_paper():
            raise RuntimeError(
                f"Broker {self.name!r} is not a paper account. PAPER-ONLY system; "
                "refusing to connect."
            )

    @abc.abstractmethod
    def get_account(self) -> Account: ...

    @abc.abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abc.abstractmethod
    def submit_limit_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        limit_price: float,
        asset_class: AssetClass,
    ) -> Order:
        """The ONLY order entry point. No market-order method exists."""

    @abc.abstractmethod
    def get_order(self, order_id: str) -> Order: ...

    @abc.abstractmethod
    def cancel_order(self, order_id: str) -> None: ...

    @abc.abstractmethod
    def list_open_orders(self) -> list[Order]: ...

    def __enter__(self) -> "Broker":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()
