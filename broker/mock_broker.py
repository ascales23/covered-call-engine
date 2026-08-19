"""
In-memory paper Broker for tests/dry-runs. Limit orders fill immediately at the
limit price (deterministic). Cash accounting mirrors a covered-call account:
selling a call credits premium and creates a short option position; buying to
close debits; equity buys/sells move shares and cash.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timezone

from broker.base import (
    Account, AssetClass, Broker, Order, OrderSide, OrderStatus, Position,
)

_OPTION_MULTIPLIER = 100.0


class MockBroker(Broker):
    name = "mock-paper"

    def __init__(self, starting_cash: float = 13000.0):
        self._cash = starting_cash
        # symbol -> (qty, avg_entry_price, asset_class)
        self._positions: dict[str, tuple[float, float, AssetClass]] = {}
        self._orders: dict[str, Order] = {}
        self._ids = itertools.count(1)
        self._connected = False

    def connect(self) -> None:
        self.assert_paper()
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_paper(self) -> bool:
        return True

    def get_account(self) -> Account:
        # equity = cash + signed position value at entry (mock has no live marks)
        pos_val = sum(qty * px * (_OPTION_MULTIPLIER if ac == AssetClass.OPTION else 1.0)
                      for qty, px, ac in self._positions.values())
        equity = self._cash + pos_val
        return Account(cash=self._cash, equity=equity, buying_power=self._cash)

    def get_positions(self) -> list[Position]:
        return [Position(sym, qty, px, ac)
                for sym, (qty, px, ac) in self._positions.items() if qty != 0]

    def submit_limit_order(self, symbol: str, qty: float, side: OrderSide,
                           limit_price: float, asset_class: AssetClass) -> Order:
        if qty <= 0:
            raise ValueError("qty must be positive")
        if limit_price <= 0:
            raise ValueError("limit_price must be positive (limit orders only)")
        mult = _OPTION_MULTIPLIER if asset_class == AssetClass.OPTION else 1.0
        notional = qty * limit_price * mult
        signed = qty if side == OrderSide.BUY else -qty

        # cash: buying debits, selling credits
        self._cash += -notional if side == OrderSide.BUY else notional
        self._apply_fill(symbol, signed, limit_price, asset_class)

        oid = str(next(self._ids))
        order = Order(
            id=oid, symbol=symbol, qty=qty, side=side, limit_price=limit_price,
            asset_class=asset_class, status=OrderStatus.FILLED,
            filled_qty=qty, filled_avg_price=limit_price,
            submitted_at=datetime.now(timezone.utc),
        )
        self._orders[oid] = order
        return order

    def _apply_fill(self, symbol: str, signed_qty: float, price: float,
                    asset_class: AssetClass) -> None:
        cur_qty, cur_px, _ = self._positions.get(symbol, (0.0, 0.0, asset_class))
        new_qty = cur_qty + signed_qty
        if abs(new_qty) < 1e-9:
            self._positions.pop(symbol, None)
            return
        # weighted-average entry only when adding in the same direction
        if cur_qty == 0 or (cur_qty > 0) == (signed_qty > 0):
            new_px = (abs(cur_qty) * cur_px + abs(signed_qty) * price) / abs(new_qty)
        else:
            new_px = cur_px  # reducing/closing keeps original basis
        self._positions[symbol] = (new_qty, new_px, asset_class)

    def get_order(self, order_id: str) -> Order:
        return self._orders[order_id]

    def cancel_order(self, order_id: str) -> None:
        o = self._orders.get(order_id)
        if o and o.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
            self._orders[order_id] = Order(
                **{**o.__dict__, "status": OrderStatus.CANCELED})

    def list_open_orders(self) -> list[Order]:
        return [o for o in self._orders.values()
                if o.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)]

    # test conveniences ------------------------------------------------------
    @property
    def cash(self) -> float:
        return self._cash

    def position_qty(self, symbol: str) -> float:
        return self._positions.get(symbol, (0.0, 0.0, None))[0]
