"""Covers strike selection, cap stepping, economics guards, roll/assignment,
reinvest, and the plan_cycle orchestration, all on deterministic inputs."""
from datetime import date, datetime, timezone, timedelta

import pytest

from data.base import OptionQuote
from data.mock_feed import build_synthetic_chain
from strategy import covered_call as cc
from strategy.covered_call import ActionType, Holdings, ShortCallView

from tests.conftest import make_config

ASOF = date(2026, 6, 17)
TS = datetime(2026, 6, 17, 17, 0, tzinfo=timezone.utc)


def mkq(strike, dte, bid, ask, *, delta=None, iv=None, spot=28.0, right="C"):
    return OptionQuote(
        occ_symbol=f"SCHD{strike}-{dte}", underlying="SCHD",
        expiry=ASOF + timedelta(days=dte), strike=float(strike), right=right,
        bid=bid, ask=ask, underlying_price=spot, as_of=TS, delta=delta, iv=iv,
    )


# ── strike selection ─────────────────────────────────────────────────────────
def test_selects_target_otm_at_nearest_expiry(cfg):
    spot = 28.0
    chain = build_synthetic_chain(
        "SCHD", spot, [ASOF + timedelta(14), ASOF + timedelta(30), ASOF + timedelta(60)],
        ASOF, iv=0.18, as_of_ts=TS,
    )
    sel = cc.select_call(spot, chain, cfg, ASOF)
    assert sel.ok, sel.reason
    assert sel.quote.dte(ASOF) == 30                     # nearest to target_dte
    assert sel.quote.strike >= spot * (1 + cfg.otm_pct / 100)  # at/above OTM target
    assert sel.prob_itm <= cfg.assignment_cap
    assert sel.limit_price == round(sel.quote.mid, 2)


def test_steps_further_otm_when_cap_exceeded(cfg):
    spot = 28.0
    calls = [
        mkq(29, 30, 0.40, 0.44, delta=0.45),   # closest to 28.70 but too risky
        mkq(30, 30, 0.20, 0.23, delta=0.28),   # first under the 0.30 cap
        mkq(31, 30, 0.10, 0.12, delta=0.18),
    ]
    sel = cc.select_call(spot, calls, cfg, ASOF)
    assert sel.ok
    assert sel.quote.strike == 30.0
    assert sel.prob_itm == 0.28


def test_skip_when_premium_below_min(cfg):
    calls = [mkq(29, 30, 0.01, 0.03, delta=0.20)]   # mid 0.02 -> $2 < $5
    sel = cc.select_call(28.0, calls, cfg, ASOF)
    assert not sel.ok
    assert "premium" in sel.reason


def test_skip_when_spread_too_wide(cfg):
    calls = [mkq(29, 30, 0.10, 0.50, delta=0.20)]   # mid .30 spread .40 -> 133%
    sel = cc.select_call(28.0, calls, cfg, ASOF)
    assert not sel.ok
    assert "spread" in sel.reason


def test_skip_when_no_expiry_in_window(cfg):
    calls = [mkq(29, 10, 0.3, 0.32, delta=0.2), mkq(29, 60, 0.5, 0.52, delta=0.2)]
    sel = cc.select_call(28.0, calls, cfg, ASOF)
    assert not sel.ok
    assert "DTE window" in sel.reason


# ── prob-ITM proxy ───────────────────────────────────────────────────────────
def test_prob_itm_prefers_delta(cfg):
    q = mkq(30, 30, 0.2, 0.23, delta=0.27)
    assert cc.prob_itm_proxy(q, cfg, ASOF) == 0.27


def test_prob_itm_bs_fallback_from_iv(cfg):
    q = mkq(30, 30, 0.2, 0.23, delta=None, iv=0.20)
    p = cc.prob_itm_proxy(q, cfg, ASOF)
    assert p is not None and 0.0 < p < 0.5      # OTM -> below 0.5


def test_prob_itm_bs_fallback_from_price(cfg):
    # no delta, no iv -> imply vol from mid, then prob
    q = mkq(30, 30, 0.18, 0.22, delta=None, iv=None, spot=28.0)
    p = cc.prob_itm_proxy(q, cfg, ASOF)
    assert p is not None and 0.0 < p < 1.0


# ── roll / assignment ────────────────────────────────────────────────────────
def _short(strike, dte):
    return ShortCallView(f"SC{strike}", float(strike), ASOF + timedelta(dte), 0.25)


def test_roll_holds_when_far_from_expiry(cfg):
    a = cc.roll_decision(_short(29, 20), 30.0, cfg, ASOF)
    assert a.type == ActionType.HOLD


def test_roll_let_expire_when_otm(cfg):
    a = cc.roll_decision(_short(29, 3), 28.0, cfg, ASOF)
    assert a.type == ActionType.LET_EXPIRE


def test_roll_up_out_when_itm_and_enabled(cfg):
    a = cc.roll_decision(_short(29, 3), 30.0, cfg, ASOF)
    assert a.type == ActionType.ROLL_UP_OUT


def test_allow_assignment_when_itm_and_roll_disabled():
    cfg = make_config(roll_if_itm=False)
    a = cc.roll_decision(_short(29, 3), 30.0, cfg, ASOF)
    assert a.type == ActionType.ALLOW_ASSIGNMENT


# ── reinvest ─────────────────────────────────────────────────────────────────
def test_reinvest_whole_shares(cfg):
    assert cc.shares_to_reinvest(100.0, 28.0, cfg) == 3
    assert cc.shares_to_reinvest(27.0, 28.0, cfg) == 0


def test_reinvest_disabled():
    cfg = make_config(reinvest_income=False)
    assert cc.shares_to_reinvest(1000.0, 28.0, cfg) == 0


# ── plan_cycle orchestration ─────────────────────────────────────────────────
def test_plan_buys_shares_when_short_base(cfg):
    a = cc.plan_cycle(Holdings(shares=0, cash=13000.0), 28.0, [], cfg, ASOF)
    assert a.type == ActionType.BUY_SHARES and a.shares_to_buy == 100


def test_plan_writes_when_holding_base(cfg):
    chain = build_synthetic_chain("SCHD", 28.0, [ASOF + timedelta(30)], ASOF,
                                  iv=0.18, as_of_ts=TS)
    a = cc.plan_cycle(Holdings(shares=100, cash=500.0), 28.0, chain, cfg, ASOF)
    assert a.type == ActionType.WRITE and a.selection.ok


def test_plan_manages_existing_short(cfg):
    h = Holdings(shares=100, cash=500.0, short_call=_short(29, 3))
    a = cc.plan_cycle(h, 30.0, [], cfg, ASOF)
    assert a.type == ActionType.ROLL_UP_OUT


def test_plan_skips_write_when_no_strike(cfg):
    h = Holdings(shares=100, cash=500.0)
    a = cc.plan_cycle(h, 28.0, [mkq(29, 10, 0.3, 0.32, delta=0.2)], cfg, ASOF)
    assert a.type == ActionType.SKIP_WRITE
