"""
Alpaca market-data DataFeed (read-only). Provides spot + option chain with
greeks/IV when the account's options data feed supplies them. Used as the
fallback behind IBKR (or primary if IBKR has no OPRA entitlement).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from data.base import DataFeed, OptionQuote

logger = logging.getLogger("alpaca_feed")


def parse_occ(sym: str) -> tuple[str, date, str, float]:
    """ROOT + YYMMDD + C/P + strike*1000(8). Returns (root, expiry, right, strike)."""
    strike = int(sym[-8:]) / 1000.0
    right = sym[-9]
    yy, mm, dd = int(sym[-15:-13]), int(sym[-13:-11]), int(sym[-11:-9])
    return sym[:-15], date(2000 + yy, mm, dd), right, strike


class AlpacaFeed(DataFeed):
    name = "alpaca"

    def __init__(self, key_id: str, secret_key: str):
        self._key = key_id
        self._secret = secret_key
        self._stock = None
        self._opt = None

    def connect(self) -> None:
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.data.historical.option import OptionHistoricalDataClient
        self._stock = StockHistoricalDataClient(self._key, self._secret)
        self._opt = OptionHistoricalDataClient(self._key, self._secret)

    def disconnect(self) -> None:
        self._stock = self._opt = None

    def is_available(self) -> bool:
        return self._stock is not None and self._opt is not None

    def get_spot(self, symbol: str) -> float:
        from alpaca.data.requests import StockLatestTradeRequest
        r = self._stock.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol))
        return float(r[symbol].price)

    def get_call_chain(self, underlying: str, dte_min: int, dte_max: int,
                       asof: date | None = None) -> list[OptionQuote]:
        from alpaca.data.requests import OptionChainRequest
        from alpaca.trading.enums import ContractType
        asof = asof or date.today()
        spot = self.get_spot(underlying)
        req = OptionChainRequest(
            underlying_symbol=underlying, type=ContractType.CALL,
            expiration_date_gte=asof + timedelta(days=dte_min),
            expiration_date_lte=asof + timedelta(days=dte_max),
        )
        try:
            chain = self._opt.get_option_chain(req)
        except Exception as e:  # pragma: no cover - network
            logger.warning("Alpaca option chain failed: %s", e)
            return []

        ts = datetime.now(timezone.utc)
        quotes: list[OptionQuote] = []
        for sym, snap in chain.items():
            q = getattr(snap, "latest_quote", None)
            if not q or q.bid_price is None or q.ask_price is None:
                continue
            _, expiry, right, strike = parse_occ(sym)
            if right != "C":
                continue
            greeks = getattr(snap, "greeks", None)
            delta = getattr(greeks, "delta", None) if greeks else None
            quotes.append(OptionQuote(
                occ_symbol=sym, underlying=underlying, expiry=expiry, strike=strike,
                right="C", bid=float(q.bid_price), ask=float(q.ask_price),
                underlying_price=spot, as_of=ts, delta=delta,
                iv=getattr(snap, "implied_volatility", None),
            ))
        return quotes
