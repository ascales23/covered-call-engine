"""
IBKR DataFeed via ib_async (synchronous API). READ-ONLY:

  * connect(..., readonly=True) -> the Gateway rejects any order on this session.
  * this class is a DataFeed (no order methods) -> cannot trade by construction.

If the account lacks OPRA option-data entitlement, quotes/greeks come back NaN;
those rows are dropped and get_call_chain() returns [], so the router falls back
to Alpaca automatically. Connecting to the existing LIVE Gateway is fine because
nothing here can place an order.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone

from data.base import DataFeed, OptionQuote

logger = logging.getLogger("ibkr_feed")


def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


class IBKRFeed(DataFeed):
    name = "ibkr"

    def __init__(self, host: str, port: int, client_id: int,
                 strike_pct_window: float = 12.0, connect_timeout: float = 8.0):
        self._host, self._port, self._cid = host, port, client_id
        self._strike_window = strike_pct_window / 100.0
        self._timeout = connect_timeout
        self._ib = None

    def connect(self) -> None:
        from ib_async import IB
        self._ib = IB()
        # readonly=True => server-side order rejection on this client.
        self._ib.connect(self._host, self._port, clientId=self._cid,
                         readonly=True, timeout=self._timeout)

    def disconnect(self) -> None:
        if self._ib is not None:
            try:
                self._ib.disconnect()
            finally:
                self._ib = None

    def is_available(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    def _stock(self, symbol: str):
        from ib_async import Stock
        c = Stock(symbol, "SMART", "USD")
        self._ib.qualifyContracts(c)
        return c

    def get_spot(self, symbol: str) -> float:
        stock = self._stock(symbol)
        (t,) = self._ib.reqTickers(stock)
        for px in (t.marketPrice(), t.last, t.close):
            if _ok(px) and px > 0:
                return float(px)
        raise RuntimeError(f"IBKR: no usable price for {symbol}")

    def get_call_chain(self, underlying: str, dte_min: int, dte_max: int,
                       asof: date | None = None) -> list[OptionQuote]:
        from ib_async import Option
        asof = asof or date.today()
        try:
            stock = self._stock(underlying)
            spot = self.get_spot(underlying)
            params = self._ib.reqSecDefOptParams(underlying, "", "STK", stock.conId)
        except Exception as e:  # pragma: no cover - network
            logger.warning("IBKR chain setup failed for %s: %s", underlying, e)
            return []
        if not params:
            return []
        p = next((x for x in params if x.exchange == "SMART"), params[0])

        lo, hi = spot * (1 - self._strike_window), spot * (1 + self._strike_window)
        strikes = sorted(s for s in p.strikes if lo <= s <= hi)
        expiries = []
        for e in sorted(p.expirations):
            try:
                ed = datetime.strptime(e, "%Y%m%d").date()
            except ValueError:
                continue
            if dte_min <= (ed - asof).days <= dte_max:
                expiries.append((e, ed))
        if not strikes or not expiries:
            return []

        contracts = [Option(underlying, e, k, "C", "SMART", tradingClass=p.tradingClass)
                     for e, _ in expiries for k in strikes]
        try:
            self._ib.qualifyContracts(*contracts)
            tickers = self._ib.reqTickers(*[c for c in contracts if c.conId])
        except Exception as e:  # pragma: no cover - network
            logger.warning("IBKR reqTickers failed: %s", e)
            return []

        ts = datetime.now(timezone.utc)
        exp_map = {e: ed for e, ed in expiries}
        out: list[OptionQuote] = []
        for t in tickers:
            c = t.contract
            if not (_ok(t.bid) and _ok(t.ask)) or t.bid < 0:
                continue
            mg = t.modelGreeks
            out.append(OptionQuote(
                occ_symbol=c.localSymbol.replace(" ", "") or f"{underlying}{c.lastTradeDateOrContractMonth}C{int(c.strike*1000):08d}",
                underlying=underlying,
                expiry=exp_map.get(c.lastTradeDateOrContractMonth,
                                   datetime.strptime(c.lastTradeDateOrContractMonth, "%Y%m%d").date()),
                strike=float(c.strike), right="C",
                bid=float(t.bid), ask=float(t.ask), underlying_price=spot, as_of=ts,
                delta=float(mg.delta) if mg and _ok(mg.delta) else None,
                iv=float(mg.impliedVol) if mg and _ok(mg.impliedVol) else None,
            ))
        return out
