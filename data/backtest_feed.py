"""
Backtest DataFeed: real SCHD prices + real VIX (scaled to SCHD) drive a
Black-Scholes-priced synthetic option chain at each historical date. Inputs are
real; only the option PRICES are modeled (no real historical chains exist).

Spreads are modeled tight (~2%) so fills happen near mid, i.e. the backtest
IGNORES the wide real-world ETF-option spreads/slippage and is therefore
optimistic on transaction costs. Stated plainly in the report.
"""
from __future__ import annotations

import bisect
from datetime import date, datetime, timedelta, timezone

from data.base import DataFeed, Dividend, OptionQuote
from strategy import greeks


def third_friday(y: int, m: int) -> date:
    d = date(y, m, 1)
    first_fri = d + timedelta((4 - d.weekday()) % 7)
    return first_fri + timedelta(14)


def monthly_expiries(asof: date, dte_min: int, dte_max: int) -> list[date]:
    out = []
    for off in range(0, 4):
        ym = asof.year * 12 + (asof.month - 1) + off
        e = third_friday(ym // 12, ym % 12 + 1)
        if dte_min <= (e - asof).days <= dte_max:
            out.append(e)
    return out


class BacktestFeed(DataFeed):
    name = "backtest"

    def __init__(self, prices: dict[date, float], vix: dict[date, float],
                 iv_scale: float, r: float, q: float, div_yield: float,
                 strike_step: float = 0.5, strike_window: float = 0.15):
        self._prices = prices
        self._pdates = sorted(prices)
        self._vix = vix
        self._vdates = sorted(vix)
        self.iv_scale = iv_scale
        self.r, self.q, self.div_yield = r, q, div_yield
        self.strike_step, self.strike_window = strike_step, strike_window
        self._cur: date | None = None

    def set_date(self, d: date) -> None:
        self._cur = d

    def _asof_value(self, d: date, dates: list, table: dict) -> float:
        i = bisect.bisect_right(dates, d) - 1
        return table[dates[max(i, 0)]]

    def spot_on(self, d: date) -> float:
        return self._asof_value(d, self._pdates, self._prices)

    def iv_on(self, d: date) -> float:
        return max(self._asof_value(d, self._vdates, self._vix) / 100.0 * self.iv_scale, 0.01)

    # DataFeed API ------------------------------------------------------------
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def is_available(self) -> bool:
        return True

    def get_spot(self, symbol: str) -> float:
        return self.spot_on(self._cur)

    def get_call_chain(self, underlying: str, dte_min: int, dte_max: int,
                       asof: date | None = None) -> list[OptionQuote]:
        asof = self._cur
        spot = self.spot_on(asof)
        iv = self.iv_on(asof)
        ts = datetime.now(timezone.utc)
        lo, hi = spot * (1 - self.strike_window), spot * (1 + self.strike_window)
        base = round(spot / self.strike_step) * self.strike_step
        n = int((hi - lo) / self.strike_step) + 1
        strikes = [round(base + (k - n // 2) * self.strike_step, 2) for k in range(n)]
        out: list[OptionQuote] = []
        for e in monthly_expiries(asof, dte_min, dte_max):
            T = (e - asof).days / 365.0
            for K in strikes:
                if K <= 0:
                    continue
                mid = greeks.bs_call_price(spot, K, T, self.r, iv, self.q)
                if mid <= 0:
                    continue
                half = mid * 0.01                      # ~2% spread => fills near mid
                delta = greeks.bs_call_delta(spot, K, T, self.r, iv, self.q)
                out.append(OptionQuote(
                    occ_symbol=f"SCHD{e:%y%m%d}C{int(round(K*1000)):08d}",
                    underlying=underlying, expiry=e, strike=float(K), right="C",
                    bid=round(mid - half, 4), ask=round(mid + half, 4),
                    underlying_price=spot, as_of=ts, delta=delta, iv=iv))
        return out

    def get_option_mid(self, underlying: str, expiry: date, strike: float,
                       right: str = "C") -> float | None:
        asof = self._cur
        T = max((expiry - asof).days, 0) / 365.0
        return greeks.bs_call_price(self.spot_on(asof), strike, T, self.r,
                                    self.iv_on(asof), self.q)

    def get_dividends(self, symbol: str, start: date, end: date) -> list[Dividend]:
        out = []
        for m in (3, 6, 9, 12):
            for y in range(start.year, end.year + 1):
                ex = date(y, m, 20)
                if start < ex <= end:
                    amt = self.spot_on(ex) * self.div_yield / 4.0
                    out.append(Dividend(symbol, ex, amt))
        return out
