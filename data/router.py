"""
FeedRouter: ordered DataFeeds, first that answers wins. Lets IBKR be primary
with Alpaca as fallback (or any order from config.data_feed_order) without the
rest of the system knowing which source served a given request.
"""
from __future__ import annotations

import logging
from datetime import date

from data.base import DataFeed, Dividend, OptionQuote

logger = logging.getLogger("router")


class FeedRouter(DataFeed):
    name = "router"

    def __init__(self, feeds: list[DataFeed]):
        if not feeds:
            raise ValueError("FeedRouter needs at least one feed")
        self._feeds = feeds

    def connect(self) -> None:
        for f in self._feeds:
            try:
                f.connect()
                logger.info("connected feed: %s (available=%s)", f.name, f.is_available())
            except Exception as e:
                logger.warning("feed %s failed to connect: %s", f.name, e)

    def disconnect(self) -> None:
        for f in self._feeds:
            try:
                f.disconnect()
            except Exception:
                pass

    def is_available(self) -> bool:
        return any(f.is_available() for f in self._feeds)

    def _live(self) -> list[DataFeed]:
        return [f for f in self._feeds if f.is_available()]

    def get_spot(self, symbol: str) -> float:
        last_err: Exception | None = None
        for f in self._live():
            try:
                return f.get_spot(symbol)
            except Exception as e:
                last_err = e
                logger.warning("%s.get_spot failed: %s", f.name, e)
        raise RuntimeError(f"No feed returned spot for {symbol}: {last_err}")

    def get_call_chain(self, underlying: str, dte_min: int, dte_max: int,
                       asof: date | None = None) -> list[OptionQuote]:
        for f in self._live():
            try:
                chain = f.get_call_chain(underlying, dte_min, dte_max, asof)
            except Exception as e:
                logger.warning("%s.get_call_chain failed: %s", f.name, e)
                continue
            if chain:
                logger.info("chain for %s served by %s (%d calls)", underlying, f.name, len(chain))
                return chain
        logger.warning("no feed returned a chain for %s", underlying)
        return []

    def get_dividends(self, symbol: str, start: date, end: date) -> list[Dividend]:
        for f in self._live():
            try:
                d = f.get_dividends(symbol, start, end)
            except Exception:
                continue
            if d:
                return d
        return []
