"""
Persistent state (SQLite) + startup reconciliation against the broker.

The singleton portfolio state is stored as a JSON blob (flexible); cycle log,
cash ledger, and equity curve are proper tables for querying/CSV/reporting.

Reconciliation: the broker is authoritative for what positions actually exist.
On startup we compare our recorded shares / short call against the broker's
paper positions and adopt reality, logging any drift, so a restart mid-cycle
(or an out-of-band Alpaca action) can't desync us into acting on stale beliefs.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date

from broker.base import AssetClass, Broker

logger = logging.getLogger("state")


@dataclass
class ShortCallRecord:
    occ_symbol: str
    strike: float
    expiry: str            # ISO date
    entry_premium: float   # per-share credit
    qty: int               # contracts (positive number; position is short)
    opened_on: str

    def expiry_date(self) -> date:
        return date.fromisoformat(self.expiry)


@dataclass
class PortfolioState:
    started_on: str
    cycle_index: int = 0
    shares: int = 0
    cost_basis: float = 0.0
    cash: float = 0.0
    short_call: ShortCallRecord | None = None
    cum_premium: float = 0.0
    cum_dividends: float = 0.0
    cum_btc_cost: float = 0.0
    income_pool: float = 0.0          # premium + dividends - roll costs, awaiting reinvestment
    realized_share_pnl: float = 0.0
    assignment_count: int = 0
    benchmark_shares: float = 0.0
    benchmark_cash: float = 0.0
    benchmark_initialized: bool = False
    last_run_on: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "PortfolioState":
        d = json.loads(s)
        sc = d.pop("short_call", None)
        st = cls(**d)
        st.short_call = ShortCallRecord(**sc) if sc else None
        return st


_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT);
CREATE TABLE IF NOT EXISTS cycle_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, cycle_index INTEGER,
    action TEXT, reason TEXT, spot REAL, strike REAL, expiry TEXT,
    prob_itm REAL, premium REAL, spread_pct REAL, order_id TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, amount REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS equity_curve (
    id INTEGER PRIMARY KEY AUTOINCREMENT, asof TEXT,
    strategy_equity REAL, benchmark_equity REAL
);
"""


class StateStore:
    def __init__(self, db_path: str, starting_capital: float, started_on: str | None = None):
        self.db_path = db_path
        self.starting_capital = starting_capital
        self._started_on = started_on or date.today().isoformat()
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ── singleton state ──────────────────────────────────────────────────────
    def load(self) -> PortfolioState:
        row = self.conn.execute("SELECT data FROM state WHERE id = 1").fetchone()
        if row is None:
            st = PortfolioState(started_on=self._started_on, cash=self.starting_capital)
            self.save(st)
            return st
        return PortfolioState.from_json(row["data"])

    def save(self, st: PortfolioState) -> None:
        self.conn.execute(
            "INSERT INTO state (id, data) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            (st.to_json(),),
        )
        self.conn.commit()

    # ── append-only logs ─────────────────────────────────────────────────────
    def append_cycle(self, **kw) -> None:
        cols = ("ts", "cycle_index", "action", "reason", "spot", "strike", "expiry",
                "prob_itm", "premium", "spread_pct", "order_id", "detail")
        self.conn.execute(
            f"INSERT INTO cycle_log ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            tuple(kw.get(c) for c in cols),
        )
        self.conn.commit()

    def append_ledger(self, ts: str, kind: str, amount: float, note: str = "") -> None:
        self.conn.execute("INSERT INTO ledger (ts, kind, amount, note) VALUES (?,?,?,?)",
                          (ts, kind, amount, note))
        self.conn.commit()

    def append_equity(self, asof: str, strat: float, bench: float) -> None:
        self.conn.execute(
            "INSERT INTO equity_curve (asof, strategy_equity, benchmark_equity) VALUES (?,?,?)",
            (asof, strat, bench))
        self.conn.commit()

    def cycles(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM cycle_log ORDER BY id").fetchall()

    def ledger_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM ledger ORDER BY id").fetchall()

    def equity_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM equity_curve ORDER BY id").fetchall()

    def close(self) -> None:
        self.conn.close()


def reconcile(state: PortfolioState, broker: Broker, underlying: str) -> PortfolioState:
    """
    Compare engine state to the broker's actual paper positions BEFORE acting.

    The engine's SQLite state is authoritative for P&L accounting (we chose
    engine-owned settlement). So this LOGS drift rather than overwriting cash or
    clearing the engine's deterministically-settled short call, except on a
    brand-new account (cycle 0), where we SEED from whatever the broker holds.
    A persistent drift warning is the signal to investigate (e.g. a crash
    between order fill and state save), not something to auto-paper-over.
    """
    positions = broker.get_positions()
    eq = {p.symbol: p for p in positions if p.asset_class == AssetClass.EQUITY}
    opt = {p.symbol: p for p in positions if p.asset_class == AssetClass.OPTION}
    broker_shares = int(sum(p.qty for s, p in eq.items() if s == underlying))
    short_syms = [s for s, p in opt.items() if p.qty < 0]

    fresh = state.cycle_index <= 1 and state.shares == 0 and state.short_call is None
    if fresh and (broker_shares or short_syms):
        logger.warning("Reconcile: seeding fresh engine state from broker "
                       "(%d sh, short=%s)", broker_shares, short_syms)
        state.shares = broker_shares
        if broker_shares and underlying in eq:
            state.cost_basis = eq[underlying].avg_entry_price
        return state

    if broker_shares != state.shares:
        logger.warning("Reconcile DRIFT: shares engine=%d broker=%d (engine "
                       "authoritative; investigate if persistent)", state.shares, broker_shares)
    rec = state.short_call.occ_symbol if state.short_call else None
    if rec and rec not in short_syms:
        logger.warning("Reconcile DRIFT: engine short call %s not at broker "
                       "(engine settles deterministically at expiry)", rec)
    if not rec and short_syms:
        logger.warning("Reconcile DRIFT: broker short call(s) %s untracked by engine", short_syms)
    return state
