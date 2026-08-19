"""End-to-end cycle on mocks (zero network): establish -> write -> roll OTM ->
assignment -> rebuy, asserting cash mechanics, capped (no-double-count) P&L,
and the kill switch."""
from datetime import date, timedelta

from broker.mock_broker import MockBroker
from data.mock_feed import MockFeed, build_synthetic_chain
from engine.runner import CoveredCallRunner
from engine.state import StateStore
from broker.base import AssetClass

from tests.conftest import make_config

D0 = date(2026, 6, 17)


def _runner(cfg, feed, broker, store):
    return CoveredCallRunner(cfg, feed, broker, store, fill_wait_s=0.0, fill_polls=1)


def _setup(cfg):
    store = StateStore(":memory:", cfg.starting_capital, started_on=D0.isoformat())
    feed = MockFeed(spot=28.0)
    broker = MockBroker(starting_cash=cfg.starting_capital)
    broker.connect()
    return store, feed, broker, _runner(cfg, feed, broker, store)


def test_full_lifecycle():
    cfg = make_config()
    store, feed, broker, runner = _setup(cfg)

    # ── cycle 1: establish 100 shares + write a call ─────────────────────────
    feed.set_spot(28.0)
    feed.set_chain(build_synthetic_chain("SCHD", 28.0, [D0 + timedelta(30), D0 + timedelta(60)], D0))
    r1 = runner.run_once(asof=D0)
    st = store.load()
    assert st.shares == 100
    assert st.short_call is not None
    assert st.cum_premium > 0
    assert broker.position_qty("SCHD") == 100
    assert broker.position_qty(st.short_call.occ_symbol) == -1   # short 1 contract
    assert st.benchmark_initialized and st.benchmark_shares == int(cfg.starting_capital // 28.0)
    # equity consistent with cash + marks (no double counting by construction)
    liab = feed.get_option_mid("SCHD", date.fromisoformat(st.short_call.expiry),
                               st.short_call.strike, "C") * 100
    assert abs(r1.strategy_equity - (st.cash + st.shares * 28.0 - liab)) < 1e-6

    # ── cycle 2: near expiry & OTM -> let-expire (BTC ~0) + rewrite ──────────
    asof2 = D0 + timedelta(27)            # 3 DTE on the original call
    feed.set_spot(28.0)
    feed.set_chain(build_synthetic_chain("SCHD", 28.0,
                   [asof2 + timedelta(30), asof2 + timedelta(60)], asof2))
    runner.run_once(asof=asof2)
    st = store.load()
    assert st.short_call is not None
    assert st.short_call.expiry == (asof2 + timedelta(30)).isoformat()   # rewritten further out
    assert st.cum_btc_cost >= 0
    rolled_strike = st.short_call.strike
    rolled_expiry = date.fromisoformat(st.short_call.expiry)
    cost_basis_before = st.cost_basis

    # ── cycle 3: expiry day, ITM -> deterministic ASSIGNMENT, then rebuy ─────
    asof3 = rolled_expiry
    spot3 = rolled_strike + 3.0           # clearly ITM
    feed.set_spot(spot3)
    feed.set_chain(build_synthetic_chain("SCHD", spot3,
                   [asof3 + timedelta(30), asof3 + timedelta(60)], asof3))
    runner.run_once(asof=asof3)
    st = store.load()
    assert st.assignment_count == 1
    # capped P&L: realized at STRIKE, never the full ITM upside
    expected_capped = (rolled_strike - cost_basis_before) * 100
    assert abs(st.realized_share_pnl - expected_capped) < 1e-6
    assert st.realized_share_pnl < (spot3 - cost_basis_before) * 100   # strictly capped
    # base rebought; a few extra shares may come from reinvested premium income,
    # but NOWHERE near a full cash-sweep (reserve capital stays as cash)
    assert 100 <= st.shares <= 105
    assert st.cash > 0.5 * cfg.starting_capital   # reserve preserved, not swept into shares


def test_kill_switch_blocks_orders(tmp_path):
    cfg = make_config(project_dir=tmp_path)
    (tmp_path / cfg.kill_switch_file).write_text("halt")
    store, feed, broker, runner = _setup(cfg)
    feed.set_spot(28.0)
    feed.set_chain(build_synthetic_chain("SCHD", 28.0, [D0 + timedelta(30)], D0))
    r = runner.run_once(asof=D0)
    assert r.skipped_for_kill_switch
    assert broker.get_positions() == []   # nothing submitted
    assert store.load().short_call is None
