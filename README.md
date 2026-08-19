# Covered-Call Paper-Trading System

A **paper-only** systematic covered-call forward-tester. Holds ~100 shares of a
broad-market ETF, sells ~30-DTE calls a configurable % OTM, rolls/re-establishes
each cycle, reinvests income, and tracks performance vs. a buy-and-hold benchmark.

> **Paper only. No live orders, ever.** This is enforced in code (see *Safety*).

## Backtest results (the honest read)

A 10-year backtest (`backtest.py`, 2016 to 2026) reuses the live engine against **real SCHD
daily prices and real VIX**, with option prices Black-Scholes-modeled because no real historical
option chains exist. The whole result rides on one assumption: whether you actually sell above
realized vol (the variance risk premium).

| Assumption (VRP uplift)             | Strategy CAGR | Buy-hold CAGR | Net premium | Max drawdown       |
|-------------------------------------|:-------------:|:-------------:|:-----------:|:------------------:|
| 1.15 (sell ~15% above realized vol) | **11.92%**    | 10.05%        | **+$15,435**| -30.8% vs -33.1%   |
| 1.00 (sell *at* realized vol)       | 10.03%        | 10.05%        | +$700       | -31.8% vs -33.1%   |

The edge is **real but small and fragile to costs**: it hinges entirely on collecting a variance
risk premium, and if you price rolls at realized vol it essentially vanishes. Fills are near-mid
and ignore real option spreads, so read it as optimistic. Knowing exactly where the edge lives,
and where it does not, is the point.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]" pandas pyarrow
cp .env.example .env         # dummy PK placeholder passes the safety gate; the backtest never trades
python backtest.py           # default VRP 1.15
python backtest.py --vrp 1.0 # price rolls at realized vol -> edge disappears
pytest -q                    # 32 tests, zero network
```

## What runs where

- **Execution:** Alpaca **paper** (`alpaca-py`), equities + single-leg options. The
  *only* component that places orders. Limit orders only; no market-order code path.
- **Market data:** IBKR via `ib_async` (read-only), Alpaca data as fallback, behind a
  `DataFeed` router. The IBKR adapter has **no order methods** and connects with
  `readonly=True`, so it cannot trade by construction.
- **Settlement:** engine-owned and deterministic (`engine/settlement.py`), *we* resolve
  expiry/assignment/dividends, Alpaca only fills orders. Keeps the forward-test
  reproducible instead of depending on the paper simulator's corporate-action handling.

## Deliberate deviations from the original spec (read these)

1. **IBKR uses the existing LIVE Gateway, read-only.** The spec asked for a *paper* IBKR
   session and an account-prefix assertion. We instead connect read-only to the running
   live Gateway on `127.0.0.1:4001` for **data only**, nothing here can place an IBKR
   order (no order methods + `readonly=True`). The paper-account assertion is therefore
   replaced by structural read-only enforcement.
2. **Alpaca serves the option chains in practice.** The IBKR account lacks live SCHD/OPRA
   market-data entitlement (error 10089), so the router falls back to Alpaca for chains.
   Alpaca option snapshots currently return **no greeks/IV**, so prob-ITM is computed via
   the Black-Scholes implied-vol fallback in `strategy/greeks.py`. Everything still works;
   selection is just model-derived rather than vendor-delta-derived.
3. **Engine accounting is authoritative.** Because settlement is engine-owned, reconcile
   *logs* drift vs. the broker rather than overwriting engine cash. The engine's SQLite
   state is the report-of-record; the Alpaca paper account's own cash may differ.
4. **Cash drag.** With `starting_capital=$13,000` and a $28 underlying, 100 shares uses
   only ~$2.8k; the rest sits as cash and only **income** (premium+dividends) is
   reinvested. The benchmark is 100% invested, so expect the strategy to trail on a
   straight-up tape, that's a real finding. Tune `starting_capital` / `contracts_per_cycle`
   if you want less drag.

## Setup

```bash
cd covered-call-engine
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"     # adds alpaca-py, ib_async, pytest
```

Secrets live in `.env` (git-ignored, chmod 600). Required vars:

```
ALPACA_API_KEY_ID=PK...          # PAPER key (PK prefix), startup refuses non-PK
ALPACA_API_SECRET_KEY=...
ALPACA_PAPER_ENDPOINT=https://paper-api.alpaca.markets
IBKR_HOST=127.0.0.1
IBKR_PORT=4001                   # read-only data off the live Gateway
IBKR_CLIENT_ID=31
```

IBKR Gateway must be logged in (IBC). The puller's Gateway on this box already is.

## Run

```bash
make test                 # unit + integration (zero network)
make verify               # read-only: Alpaca options level, feed availability, sample chain
python main.py --no-orders   # full decision cycle + report, submits nothing
make run                  # one live paper cycle (orders only during market hours)
```

`main.py` is idempotent and safe to re-run (cron-style). Orders are submitted only when
the market is open unless `--force-orders` is given. State persists to
`covered_call.sqlite`; reports append to `reports/cycles.csv`.

## Reading the report

`render_console` / `reports/cycles.csv` show, each cycle:

- position (shares, cost basis, open short call + mark), cash;
- `cum_premium`, `cum_btc_cost`, `net_premium`, `cum_dividends`, `realized_share_pnl`;
- `assignment_count`, realized assignment rate vs. the 0.30 `assignment_cap` target;
- annualized income yield;
- **strategy equity vs. benchmark equity** (the headline), and outperformance.

**No double counting:** equity = cash + share marks − short-call liability, so premium and
(capped) price appreciation never overlap. On assignment, share P&L is realized at the
*strike* (capped); premium was booked separately when the call was sold.

## Safety invariants (enforced in code)

- Paper-only gate at load (`config.validate`): rejects `i_understand_this_is_live=true`,
  any non-paper Alpaca endpoint, and non-`PK` keys.
- `Broker` has no market-order method; `DataFeed` has no order method.
- IBKR connects `readonly=True`.
- Idempotent + restartable; SQLite state + startup reconcile (logs drift).
- Kill switch: create a file named `KILL_SWITCH` in the project dir → no new orders submit,
  reporting still runs.

## Layout

```
config.py  config.yaml  main.py
data/      base.py ibkr_feed.py alpaca_feed.py mock_feed.py router.py
broker/    base.py alpaca_paper.py mock_broker.py
strategy/  covered_call.py greeks.py
engine/    scheduler.py runner.py state.py settlement.py
reporting/ metrics.py dashboard.py
tests/     greeks, covered_call, config_safety, integration
```

## Limitations

- Market-hours gate is weekend+RTH only (no holiday calendar), fine for paper.
- A crash between order fill and state-save may need manual reconciliation (rare); the
  drift warning is the signal.
- IBKR delayed-data / OPRA entitlement would let IBKR (vendor greeks) serve chains; today
  Alpaca + BS fallback does.

## License

MIT. See [LICENSE](LICENSE).

Author: **Alexander Scales**
