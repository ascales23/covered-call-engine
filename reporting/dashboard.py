"""Console table + appended CSV. Streamlit is an optional extra (not required)."""
from __future__ import annotations

import csv
import os

from reporting.metrics import Summary

_ROWS = [
    ("As of", "asof"), ("Underlying", "underlying"), ("Spot", "spot"),
    ("Shares", "shares"), ("Cost basis", "cost_basis"), ("Cash", "cash"),
    ("Short call", "short_call"),
    ("Cum premium", "cum_premium"), ("Cum BTC cost", "cum_btc_cost"),
    ("Net premium", "net_premium"), ("Cum dividends", "cum_dividends"),
    ("Realized share P&L", "realized_share_pnl"),
    ("Assignments", "assignment_count"), ("Writes", "writes"),
    ("Realized assignment rate", "realized_assignment_rate"),
    ("Annualized income yield", "annualized_income_yield"),
    ("Strategy equity", "strategy_equity"), ("Benchmark equity", "benchmark_equity"),
    ("Strategy return", "strategy_return"), ("Benchmark return", "benchmark_return"),
    ("Outperformance (of capital)", "outperformance"),
]
_PCT = {"realized_assignment_rate", "annualized_income_yield",
        "strategy_return", "benchmark_return", "outperformance"}


def _fmt(key, val):
    if val is None:
        return "-"
    if key in _PCT:
        return f"{val * 100:+.2f}%"
    if isinstance(val, float):
        return f"{val:,.2f}"
    return str(val)


def render_console(s: Summary) -> str:
    width = max(len(lbl) for lbl, _ in _ROWS) + 2
    lines = ["", "── Covered-Call Forward Test ─────────────────────────────"]
    for label, key in _ROWS:
        lines.append(f"  {label:<{width}} {_fmt(key, getattr(s, key))}")
    lines.append("──────────────────────────────────────────────────────────")
    out = "\n".join(lines)
    print(out)
    return out


def append_csv(path: str, s: Summary) -> None:
    row = s.as_row()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new:
            w.writeheader()
        w.writerow(row)
