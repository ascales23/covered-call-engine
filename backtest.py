"""
Long-term covered-call backtest. Drives the SAME engine the live system uses,
stepping `asof` through 10y of real SCHD prices, with options priced by
Black-Scholes from real VIX (scaled to SCHD's vol level + a variance-risk-premium
uplift). Honest caveats printed with the results.

    python backtest.py --capital 50000
"""
from __future__ import annotations

import argparse
import glob
import logging
import math
import os
import statistics
from dataclasses import replace
from datetime import date

import pandas as pd

from config import load_config
from broker.mock_broker import MockBroker
from data.backtest_feed import BacktestFeed
from engine.runner import CoveredCallRunner
from engine.state import StateStore
from reporting import metrics

logging.basicConfig(level=logging.ERROR)   # silence per-day reconcile drift logs

SCHD_PARQUET = "backtest_data/schd_daily.parquet"
VIX_GLOB = "backtest_data/vix_daily.parquet"   # bundled; was ~/marketdata/vix_index/VIX/1day/*.parquet
VRP_UPLIFT = 1.15   # implied > realized: the premium covered calls actually harvest


def load_prices(path: str) -> dict[date, float]:
    df = pd.read_parquet(path)
    return {date.fromisoformat(str(d)[:10]): float(c) for d, c in zip(df["date"], df["close"])}


def load_vix(globpat: str) -> dict[date, float]:
    files = sorted(glob.glob(os.path.expanduser(globpat)))
    df = pd.concat(pd.read_parquet(f, columns=["date", "close"]) for f in files)
    df["d"] = pd.to_datetime(df["date"], utc=True).dt.date
    return {d: float(c) for d, c in zip(df["d"], df["close"])}


def realized_vol_series(prices: dict[date, float], window: int = 21) -> dict[date, float]:
    dts = sorted(prices)
    rets = [math.log(prices[dts[i]] / prices[dts[i - 1]]) for i in range(1, len(dts))]
    out = {}
    for i in range(window, len(rets)):
        w = rets[i - window:i]
        out[dts[i + 1]] = statistics.pstdev(w) * math.sqrt(252)
    return out


def calibrate_scale(prices, vix, vrp: float) -> tuple[float, float]:
    """raw scale = median(SCHD realized / (VIX/100)); applied = raw * VRP uplift."""
    rv = realized_vol_series(prices)
    ratios = [rv[d] / (vix[d] / 100.0) for d in rv if d in vix and vix[d] > 0]
    raw = statistics.median(ratios)
    return raw, raw * vrp


def stats(curve: list[tuple[date, float]]) -> dict:
    eq = [v for _, v in curve]
    days = (curve[-1][0] - curve[0][0]).days or 1
    cagr = (eq[-1] / eq[0]) ** (365.0 / days) - 1.0
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    return {"final": eq[-1], "cagr": cagr, "max_drawdown": mdd}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=50000)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--vrp", type=float, default=VRP_UPLIFT,
                    help="variance-risk-premium uplift on implied vol (1.0 = price at realized)")
    ap.add_argument("--plot", default="backtest_data/equity_curve.png")
    args = ap.parse_args()

    prices = load_prices(SCHD_PARQUET)
    vix = load_vix(VIX_GLOB)
    raw_scale, scale = calibrate_scale(prices, vix, args.vrp)

    pdates = sorted(prices)
    start = date.fromisoformat(args.start) if args.start else pdates[30]
    end = date.fromisoformat(args.end) if args.end else pdates[-1]
    sim_dates = [d for d in pdates if start <= d <= end]
    start_px = prices[sim_dates[0]]
    n_contracts = max(1, int(args.capital // (100 * start_px)))

    base = load_config("config.yaml")
    cfg = replace(base, starting_capital=args.capital, contracts_per_cycle=n_contracts,
                  underlying="SCHD", db_path=":memory:")
    feed = BacktestFeed(prices, vix, scale, cfg.risk_free_rate, cfg.dividend_yield,
                        cfg.dividend_yield)
    broker = MockBroker(args.capital); broker.connect()
    store = StateStore(":memory:", args.capital, started_on=sim_dates[0].isoformat())
    runner = CoveredCallRunner(cfg, feed, broker, store, fill_wait_s=0.0, fill_polls=1)

    print(f"Backtest SCHD {sim_dates[0]} -> {sim_dates[-1]}  ({len(sim_dates)} trading days)")
    print(f"  capital ${args.capital:,.0f}  start px {start_px:.2f}  -> {n_contracts} "
          f"contracts ({n_contracts*100} sh base)")
    print(f"  IV model: VIX/100 x {raw_scale:.2f} (SCHD/SPX vol) x {args.vrp} (VRP) "
          f"= x{scale:.2f}")

    for d in sim_dates:
        feed.set_date(d)
        runner.run_once(asof=d, allow_orders=True)

    st = store.load()
    last_px = prices[sim_dates[-1]]
    s = metrics.summarize(store, st, last_px, args.capital, "SCHD", sim_dates[-1])

    curve = [(date.fromisoformat(r["asof"]), r["strategy_equity"]) for r in store.equity_rows()]
    bcurve = [(date.fromisoformat(r["asof"]), r["benchmark_equity"]) for r in store.equity_rows()]
    sst, bst = stats(curve), stats(bcurve)

    print("\n── Long-term result (modeled options; real SCHD + VIX inputs) ──")
    print(f"  Strategy : final ${sst['final']:,.0f}  CAGR {sst['cagr']*100:5.2f}%  "
          f"maxDD {sst['max_drawdown']*100:6.2f}%")
    print(f"  BuyHold  : final ${bst['final']:,.0f}  CAGR {bst['cagr']*100:5.2f}%  "
          f"maxDD {bst['max_drawdown']*100:6.2f}%")
    print(f"  Premium collected (gross): ${st.cum_premium:,.0f}  "
          f"net of rolls: ${st.cum_premium - st.cum_btc_cost:,.0f}")
    print(f"  Dividends: ${st.cum_dividends:,.0f}   Assignments: {st.assignment_count} "
          f"over {s.writes} writes ({s.realized_assignment_rate*100:.1f}%)")
    print(f"  Annualized income yield (net premium/capital): {s.annualized_income_yield*100:.2f}%")
    print("\n  CAVEATS: option prices are Black-Scholes-modeled (no real historical chains "
          "exist);\n  fills are near-mid (ignores SCHD's wide real option spreads/slippage); "
          "fixed\n  contract count (coverage drifts as reinvested shares accumulate). "
          "Treat as\n  indicative, optimistic on costs.")

    out_csv = "backtest_data/equity_curve.csv"
    pd.DataFrame({"date": [d for d, _ in curve],
                  "strategy": [v for _, v in curve],
                  "benchmark": [v for _, v in bcurve]}).to_csv(out_csv, index=False)
    print(f"\n  equity curve -> {out_csv}")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(11, 6))
        ax.plot([d for d, _ in curve], [v for _, v in curve], label="Covered call", lw=1.5)
        ax.plot([d for d, _ in bcurve], [v for _, v in bcurve], label="Buy & hold SCHD",
                lw=1.5, alpha=0.8)
        ax.set_title(f"SCHD covered-call backtest  (${args.capital:,.0f} start, "
                     f"{sim_dates[0].year}-{sim_dates[-1].year})")
        ax.set_ylabel("Portfolio value ($)"); ax.legend(); ax.grid(alpha=0.3)
        fig.tight_layout(); fig.savefig(args.plot, dpi=110)
        print(f"  chart -> {args.plot}")
    except Exception as e:
        print(f"  (plot skipped: {e})")


if __name__ == "__main__":
    main()
