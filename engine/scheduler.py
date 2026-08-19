"""
Cycle timing. The runner is designed to be invoked once per call (cron-style)
and is idempotent, so this stays minimal: a market-open gate used to decide
whether live order submission is sensible. Holidays are NOT modeled (weekend +
RTH only), fine for a paper forward-test; noted in the README.
"""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


def now_et() -> datetime:
    return datetime.now(ET)


def is_market_open(dt: datetime | None = None) -> bool:
    dt = dt or now_et()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    if dt.weekday() >= 5:
        return False
    return RTH_OPEN <= dt.timetz().replace(tzinfo=None) <= RTH_CLOSE
