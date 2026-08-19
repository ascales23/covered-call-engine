"""
Performance metrics. The headline is strategy equity vs. buy-and-hold benchmark.

No-double-counting note: equity is computed from actual cash + share marks minus
the short-call liability, so premium and (capped) price appreciation can never be
counted twice. The attribution buckets below are reported separately for clarity:
premium income, dividend income, and realized share P&L (capped at strike on
assignment) are distinct, non-overlapping credits.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from engine.state import PortfolioState, StateStore


def short_call_liability(state: PortfolioState, spot: float) -> float:
    sc = state.short_call
    if not sc:
        return 0.0
    return max(spot - sc.strike, 0.0) * 100 * sc.qty   # intrinsic mark


@dataclass
class Summary:
    asof: str
    underlying: str
    spot: float
    shares: int
    cost_basis: float
    cash: float
    short_call: str | None
    cum_premium: float
    cum_btc_cost: float
    net_premium: float
    cum_dividends: float
    realized_share_pnl: float
    assignment_count: int
    writes: int
    realized_assignment_rate: float
    elapsed_days: int
    annualized_income_yield: float
    strategy_equity: float
    benchmark_equity: float
    strategy_return: float
    benchmark_return: float
    outperformance: float

    def as_row(self) -> dict:
        return self.__dict__.copy()


def summarize(store: StateStore, state: PortfolioState, spot: float,
              starting_capital: float, underlying: str = "",
              asof: date | None = None) -> Summary:
    asof = asof or date.today()
    writes = sum(1 for r in store.ledger_rows() if r["kind"] == "premium")
    net_premium = state.cum_premium - state.cum_btc_cost
    start = date.fromisoformat(state.started_on)
    elapsed = max((asof - start).days, 1)
    ann_yield = (net_premium / starting_capital) * (365.0 / elapsed) if starting_capital else 0.0

    liab = short_call_liability(state, spot)
    strat_eq = state.cash + state.shares * spot - liab
    bench_eq = state.benchmark_shares * spot + state.benchmark_cash

    sc = state.short_call
    sc_desc = (f"{sc.occ_symbol} K={sc.strike} exp {sc.expiry} (mark {liab/ (100*sc.qty):.2f})"
               if sc else None)

    return Summary(
        asof=asof.isoformat(), underlying=underlying,
        spot=spot, shares=state.shares, cost_basis=state.cost_basis, cash=state.cash,
        short_call=sc_desc, cum_premium=state.cum_premium, cum_btc_cost=state.cum_btc_cost,
        net_premium=net_premium, cum_dividends=state.cum_dividends,
        realized_share_pnl=state.realized_share_pnl, assignment_count=state.assignment_count,
        writes=writes,
        realized_assignment_rate=(state.assignment_count / writes) if writes else 0.0,
        elapsed_days=elapsed, annualized_income_yield=ann_yield,
        strategy_equity=strat_eq, benchmark_equity=bench_eq,
        strategy_return=strat_eq / starting_capital - 1.0,
        benchmark_return=bench_eq / starting_capital - 1.0,
        outperformance=(strat_eq - bench_eq) / starting_capital,
    )
