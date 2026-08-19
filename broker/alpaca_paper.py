"""
Alpaca paper-trading Broker adapter (alpaca-py). Paper-only, limit-only.

connect() asserts the account is paper. There is no market-order path. Options
orders use the OCC contract symbol; qty is in contracts.
"""
from __future__ import annotations

import logging

from broker.base import (
    Account, AssetClass, Broker, Order, OrderSide, OrderStatus, Position,
)

logger = logging.getLogger("alpaca_paper")

_STATUS_MAP = {
    "new": OrderStatus.NEW,
    "accepted": OrderStatus.NEW,
    "pending_new": OrderStatus.NEW,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
}


class AlpacaPaperBroker(Broker):
    name = "alpaca-paper"

    def __init__(self, key_id: str, secret_key: str):
        self._key = key_id
        self._secret = secret_key
        self._client = None

    def connect(self) -> None:
        from alpaca.trading.client import TradingClient
        # paper=True targets https://paper-api.alpaca.markets
        self._client = TradingClient(self._key, self._secret, paper=True)
        self.assert_paper()  # belt-and-suspenders: verify with the account itself

    def disconnect(self) -> None:
        self._client = None

    def is_paper(self) -> bool:
        # The 'PK' key prefix + paper client is the gate; confirm the account is
        # reachable as a paper account (live keys can't auth a paper client).
        try:
            self._client.get_account()
            return self._key.startswith("PK")
        except Exception as e:  # pragma: no cover - network
            logger.error("Could not verify paper account: %s", e)
            return False

    # ── account / positions ──────────────────────────────────────────────────
    def options_trading_level(self) -> int:
        acct = self._client.get_account()
        lvl = getattr(acct, "options_trading_level", 0)
        try:
            return int(lvl)
        except (TypeError, ValueError):
            return 0

    def get_account(self) -> Account:
        a = self._client.get_account()
        return Account(cash=float(a.cash), equity=float(a.equity),
                       buying_power=float(a.buying_power))

    def get_positions(self) -> list[Position]:
        out = []
        for p in self._client.get_all_positions():
            ac = (AssetClass.OPTION if str(getattr(p, "asset_class", "")).endswith("option")
                  else AssetClass.EQUITY)
            out.append(Position(symbol=p.symbol, qty=float(p.qty),
                                avg_entry_price=float(p.avg_entry_price), asset_class=ac,
                                market_value=float(p.market_value) if p.market_value else None))
        return out

    # ── orders (limit only) ──────────────────────────────────────────────────
    def submit_limit_order(self, symbol: str, qty: float, side: OrderSide,
                           limit_price: float, asset_class: AssetClass) -> Order:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide as AOS, TimeInForce
        if limit_price <= 0 or qty <= 0:
            raise ValueError("limit_price and qty must be positive")
        req = LimitOrderRequest(
            symbol=symbol, qty=qty,
            side=AOS.BUY if side == OrderSide.BUY else AOS.SELL,
            time_in_force=TimeInForce.DAY, limit_price=round(limit_price, 2),
        )
        resp = self._client.submit_order(order_data=req)
        return self._to_order(resp, asset_class)

    def get_order(self, order_id: str) -> Order:
        resp = self._client.get_order_by_id(order_id)
        ac = (AssetClass.OPTION if str(getattr(resp, "asset_class", "")).endswith("option")
              else AssetClass.EQUITY)
        return self._to_order(resp, ac)

    def cancel_order(self, order_id: str) -> None:
        self._client.cancel_order_by_id(order_id)

    def list_open_orders(self) -> list[Order]:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        reqs = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        return [self._to_order(o, AssetClass.EQUITY) for o in self._client.get_orders(reqs)]

    def _to_order(self, r, asset_class: AssetClass) -> Order:
        status = _STATUS_MAP.get(str(r.status).split(".")[-1].lower(), OrderStatus.NEW)
        return Order(
            id=str(r.id), symbol=r.symbol, qty=float(r.qty or 0),
            side=OrderSide.BUY if str(r.side).endswith("buy") else OrderSide.SELL,
            limit_price=float(r.limit_price or 0), asset_class=asset_class, status=status,
            filled_qty=float(r.filled_qty or 0),
            filled_avg_price=float(r.filled_avg_price or 0),
            submitted_at=getattr(r, "submitted_at", None),
        )
