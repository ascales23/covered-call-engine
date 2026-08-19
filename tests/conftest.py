"""Test helpers: build a Config directly (no .env / no network)."""
from pathlib import Path

import pytest

from config import AlpacaCreds, Config, IBKRConn, PAPER_ENDPOINT


def make_config(**overrides) -> Config:
    base = dict(
        underlying="SCHD",
        starting_capital=13000.0,
        contracts_per_cycle=1,
        benchmark_mode=True,
        target_dte=30,
        dte_tolerance=7,
        otm_pct=2.5,
        assignment_cap=0.30,
        min_premium_dollars=5.0,
        max_bid_ask_spread_pct=15.0,
        roll_trigger_dte=5,
        roll_if_itm=True,
        reinvest_income=True,
        risk_free_rate=0.04,
        dividend_yield=0.035,
        data_feed_order=("ibkr", "alpaca"),
        i_understand_this_is_live=False,
        kill_switch_file="KILL_SWITCH",
        db_path=":memory:",
        report_csv="reports/cycles.csv",
        alpaca=AlpacaCreds("PKTEST", "secret", PAPER_ENDPOINT),
        ibkr=IBKRConn("127.0.0.1", 4002, 31),
        project_dir=Path("/tmp"),
    )
    base.update(overrides)
    return Config(**base)


@pytest.fixture
def cfg() -> Config:
    return make_config()
