"""
Deterministic in-memory DataFeed for tests and dry-runs. No network.

Either hand it an explicit chain, or let build_synthetic_chain() generate one
with Black-Scholes deltas so strike-selection logic can be exercised reproducibly.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from data.base import DataFeed, Dividend, OptionQuote
from strategy import greeks


def occ_symbol(underlying: str, expiry: date, right: str, strike: float) -> str:
    return f"{underlying}{expiry:%y%m%d}{right}{int(round(strike * 1000)):08d}"


def build_synthetic_chain(
    underlying: str,
    spot: float,
    expiries: list[date],
    asof: date,
    *,
    iv: float = 0.18,
    r: float = 0.04,
    q: float = 0.035,
    strike_step: float = 1.0,
    n_strikes_each_side: int = 12,
    spread_frac: float = 0.06,        # bid/ask width as fraction of mid
    as_of_ts: datetime | None = None,
) -> list[OptionQuote]:
    """Generate a call chain centered on spot with BS-consistent mids + deltas."""
    ts = as_of_ts or datetime.now(timezone.utc)
    base = round(spot / strike_step) * strike_step
    quotes: list[OptionQuote] = []
    for expiry in expiries:
        T = max((expiry - asof).days, 0) / 365.0
        for k in range(-n_strikes_each_side, n_strikes_each_side + 1):
            strike = round(base + k * strike_step, 2)
            if strike <= 0:
                continue
            mid = greeks.bs_call_price(spot, strike, T, r, iv, q)
            mid = max(mid, 0.01)
            half = mid * spread_frac / 2.0
            delta = greeks.bs_call_delta(spot, strike, T, r, iv, q)
            quotes.append(OptionQuote(
                occ_symbol=occ_symbol(underlying, expiry, "C", strike),
                underlying=underlying, expiry=expiry, strike=strike, right="C",
                bid=round(mid - half, 2), ask=round(mid + half, 2),
                underlying_price=spot, as_of=ts, delta=delta, iv=iv,
            ))
    return quotes


class MockFeed(DataFeed):
    name = "mock"

    def __init__(self, spot: float = 28.0, chain: list[OptionQuote] | None = None,
                 dividends: list[Dividend] | None = None, available: bool = True):
        self._spot = spot
        self._chain = chain or []
        self._dividends = dividends or []
        self._available = available
        self._connected = False

    # let tests mutate market state between cycles
    def set_spot(self, spot: float) -> None:
        self._spot = spot

    def set_chain(self, chain: list[OptionQuote]) -> None:
        self._chain = chain

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_available(self) -> bool:
        return self._available and self._connected

    def get_spot(self, symbol: str) -> float:
        return self._spot

    def get_call_chain(self, underlying: str, dte_min: int, dte_max: int,
                       asof: date | None = None) -> list[OptionQuote]:
        asof = asof or date.today()
        return [c for c in self._chain
                if c.right == "C" and dte_min <= c.dte(asof) <= dte_max]

    def get_dividends(self, symbol: str, start: date, end: date) -> list[Dividend]:
        return [d for d in self._dividends if start <= d.ex_date <= end]

    def get_option_mid(self, underlying, expiry, strike, right="C"):
        for q in self._chain:
            if q.right == right and q.expiry == expiry and abs(q.strike - strike) < 1e-6:
                return q.mid
        return None
