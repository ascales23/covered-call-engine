"""
Covered-call decision logic. PURE: no network, no DB, no clock, every input is
passed in, every output is an explicit, logged decision. This is the module the
tests cover most heavily; correctness here is the whole point.

Two non-overlapping return concepts:
  * select_call()  -> which call to sell this cycle (or why we skip)
  * roll_decision() -> what to do with an existing short call near expiry
plan_cycle() ties them together for the runner.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from data.base import OptionQuote
from strategy import greeks


# ── inputs the strategy reasons over ─────────────────────────────────────────
@dataclass(frozen=True)
class ShortCallView:
    """An open sold call we currently hold (short)."""
    occ_symbol: str
    strike: float
    expiry: date
    entry_premium: float          # per-share credit received when sold


@dataclass(frozen=True)
class Holdings:
    shares: int
    cash: float
    short_call: ShortCallView | None = None


# ── outputs ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class StrikeSelection:
    ok: bool
    reason: str
    quote: OptionQuote | None = None
    limit_price: float | None = None     # mid, rounded to the penny
    prob_itm: float | None = None


class ActionType(str, Enum):
    BUY_SHARES = "buy_shares"            # establish/replace the 100-share base
    WRITE = "write"                      # sell-to-open a call
    SKIP_WRITE = "skip_write"            # eligible to write but no acceptable strike
    HOLD = "hold"                        # short call still has time; do nothing
    LET_EXPIRE = "let_expire"            # OTM near expiry -> expire worthless, rewrite next
    ROLL_UP_OUT = "roll_up_out"          # ITM near expiry -> BTC and re-sell further out
    ALLOW_ASSIGNMENT = "allow_assignment"  # ITM near expiry, roll disabled -> get called away


@dataclass(frozen=True)
class CycleAction:
    type: ActionType
    reason: str
    selection: StrikeSelection | None = None   # populated for WRITE
    shares_to_buy: int = 0                      # populated for BUY_SHARES


# ── prob-ITM proxy (delta first, BS fallback) ────────────────────────────────
def prob_itm_proxy(q: OptionQuote, cfg, asof: date) -> float | None:
    """
    Estimated probability the call finishes ITM, used against assignment_cap.
    Prefer the feed's delta; fall back to Black-Scholes N(d2), implying vol from
    the mid when the feed gives no IV. Returns None if it cannot be assessed
    (the caller then refuses to write that strike, never guess on risk).
    """
    if q.delta is not None:
        return abs(q.delta)
    T = max(q.dte(asof), 0) / 365.0
    if T <= 0:
        return 1.0 if q.underlying_price > q.strike else 0.0
    sigma = q.iv
    if sigma is None:
        sigma = greeks.implied_vol_call(
            q.mid, q.underlying_price, q.strike, T, cfg.risk_free_rate, cfg.dividend_yield
        )
    if sigma is None or sigma <= 0:
        return None
    return greeks.prob_itm_call(
        q.underlying_price, q.strike, T, cfg.risk_free_rate, sigma, cfg.dividend_yield
    )


# ── strike selection ─────────────────────────────────────────────────────────
def select_call(
    spot: float, calls: list[OptionQuote], cfg, asof: date
) -> StrikeSelection:
    """Implements §3 strike-selection exactly, with auditable skip reasons."""
    in_window = [c for c in calls if cfg.dte_min <= c.dte(asof) <= cfg.dte_max]
    if not in_window:
        return StrikeSelection(False, "no expiry within DTE window")

    # expiry closest to target_dte
    best_dte = min({c.dte(asof) for c in in_window}, key=lambda d: abs(d - cfg.target_dte))
    expiry_calls = sorted((c for c in in_window if c.dte(asof) == best_dte),
                          key=lambda c: c.strike)
    if not expiry_calls:
        return StrikeSelection(False, "no calls at chosen expiry")

    target_strike = spot * (1.0 + cfg.otm_pct / 100.0)
    # index of strike closest to target, then step FURTHER OTM (higher strikes)
    start = min(range(len(expiry_calls)),
                key=lambda i: abs(expiry_calls[i].strike - target_strike))

    last_reason = "no strike satisfied assignment cap"
    for i in range(start, len(expiry_calls)):
        q = expiry_calls[i]
        p_itm = prob_itm_proxy(q, cfg, asof)
        if p_itm is None:
            last_reason = "prob-ITM unassessable (no delta/iv/price)"
            continue
        if p_itm > cfg.assignment_cap:
            last_reason = f"prob-ITM {p_itm:.3f} > cap {cfg.assignment_cap}"
            continue
        # passed the cap, now the economics guards (per CONTRACT, $/contract)
        premium_dollars = q.mid * 100.0
        if premium_dollars < cfg.min_premium_dollars or round(q.mid, 2) < 0.01:
            return StrikeSelection(
                False,
                f"premium ${premium_dollars:.2f}/contract < min ${cfg.min_premium_dollars}",
                quote=q, prob_itm=p_itm,
            )
        if q.spread_pct > cfg.max_bid_ask_spread_pct:
            return StrikeSelection(
                False,
                f"spread {q.spread_pct:.1f}% > max {cfg.max_bid_ask_spread_pct}%",
                quote=q, prob_itm=p_itm,
            )
        return StrikeSelection(
            True, "selected", quote=q, limit_price=round(q.mid, 2), prob_itm=p_itm
        )

    return StrikeSelection(False, last_reason)


# ── roll / assignment decision ───────────────────────────────────────────────
def roll_decision(short_call: ShortCallView, spot: float, cfg, asof: date) -> CycleAction:
    dte = (short_call.expiry - asof).days
    if dte > cfg.roll_trigger_dte:
        return CycleAction(ActionType.HOLD, f"{dte} DTE > roll trigger {cfg.roll_trigger_dte}")
    itm = spot > short_call.strike
    if not itm:
        return CycleAction(ActionType.LET_EXPIRE,
                           f"OTM at {dte} DTE (spot {spot:.2f} <= strike {short_call.strike:.2f})")
    if cfg.roll_if_itm:
        return CycleAction(ActionType.ROLL_UP_OUT,
                           f"ITM at {dte} DTE (spot {spot:.2f} > strike {short_call.strike:.2f}); roll up-and-out")
    return CycleAction(ActionType.ALLOW_ASSIGNMENT,
                       f"ITM at {dte} DTE; roll_if_itm=false -> allow assignment")


# ── reinvestment helper ──────────────────────────────────────────────────────
def shares_to_reinvest(cash: float, share_price: float, cfg) -> int:
    """Whole shares affordable from free cash, if reinvestment is enabled."""
    if not cfg.reinvest_income or share_price <= 0:
        return 0
    return int(cash // share_price)


# ── top-level cycle planner ──────────────────────────────────────────────────
def plan_cycle(holdings: Holdings, spot: float, calls: list[OptionQuote],
               cfg, asof: date) -> CycleAction:
    """
    One pure decision for the current cycle. The runner executes it (and handles
    the second leg of a roll by calling select_call again on the fresh chain).
    """
    base = cfg.shares_per_cycle

    # need the share base first (initial cycle, or after assignment called shares away)
    if holdings.shares < base:
        return CycleAction(ActionType.BUY_SHARES,
                           f"hold {holdings.shares} < required {base} shares",
                           shares_to_buy=base - holdings.shares)

    # existing short call -> manage it
    if holdings.short_call is not None:
        return roll_decision(holdings.short_call, spot, cfg, asof)

    # have shares, no short call -> write one
    sel = select_call(spot, calls, cfg, asof)
    if sel.ok:
        return CycleAction(ActionType.WRITE, sel.reason, selection=sel)
    return CycleAction(ActionType.SKIP_WRITE, sel.reason, selection=sel)
