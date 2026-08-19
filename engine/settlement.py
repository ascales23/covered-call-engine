"""
Deterministic settlement, the authoritative resolver for option expiry,
assignment, and dividends. Alpaca only fills our orders; THIS decides outcomes,
so the forward-test is reproducible and not at the mercy of the paper simulator.

No-double-counting is enforced here: on assignment the share P&L is realized at
the STRIKE (capped), and the premium was already booked separately when the call
was sold. The two never overlap.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from engine.state import ShortCallRecord


@dataclass(frozen=True)
class SettlementResult:
    assigned: bool
    expired_worthless: bool
    cash_delta: float          # proceeds from assignment (strike * 100 * qty)
    shares_delta: int          # negative => shares called away
    realized_share_pnl: float  # (strike - cost_basis) * shares, the CAPPED gain
    note: str


def settle_expiry(
    short_call: ShortCallRecord,
    underlying_at_expiry: float,
    cost_basis: float,
) -> SettlementResult:
    """
    Resolve a short call at/after its expiry given the settlement underlying.
      * underlying <= strike -> expires worthless (premium already kept)
      * underlying  > strike -> assignment: 100*qty shares sold at strike
    """
    qty = short_call.qty
    shares = 100 * qty
    if underlying_at_expiry <= short_call.strike:
        return SettlementResult(
            assigned=False, expired_worthless=True,
            cash_delta=0.0, shares_delta=0, realized_share_pnl=0.0,
            note=f"expired worthless (underlying {underlying_at_expiry:.2f} "
                 f"<= strike {short_call.strike:.2f})",
        )
    proceeds = short_call.strike * shares
    realized = (short_call.strike - cost_basis) * shares   # capped at strike
    return SettlementResult(
        assigned=True, expired_worthless=False,
        cash_delta=proceeds, shares_delta=-shares, realized_share_pnl=realized,
        note=f"ASSIGNED: {shares} sh called away at {short_call.strike:.2f} "
             f"(underlying {underlying_at_expiry:.2f}); capped share P&L {realized:+.2f}",
    )


@dataclass(frozen=True)
class DividendCredit:
    cash: float
    events: list[tuple[str, float]] = field(default_factory=list)  # (ex_date, amount)


def credit_dividends(shares: int, dividends) -> DividendCredit:
    """Total cash from dividends on the shares held across each ex-date."""
    total = 0.0
    events: list[tuple[str, float]] = []
    for d in dividends:
        amt = d.amount_per_share * shares
        if amt:
            total += amt
            events.append((d.ex_date.isoformat(), amt))
    return DividendCredit(cash=total, events=events)


def is_matured(short_call: ShortCallRecord, today: date) -> bool:
    """True once the short call has reached its expiry date."""
    return today >= short_call.expiry_date()
