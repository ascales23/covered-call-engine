"""
Entry point. Builds the real adapters, runs one idempotent cycle, reports.

  python main.py --verify        # read-only: check broker + feeds + entitlements
  python main.py --no-orders     # full cycle decisions + report, submit nothing
  python main.py                 # live paper cycle (orders only when market open)
"""
from __future__ import annotations

import argparse
import logging
from datetime import date

from config import load_config
from data.alpaca_feed import AlpacaFeed
from data.ibkr_feed import IBKRFeed
from data.router import FeedRouter
from broker.alpaca_paper import AlpacaPaperBroker
from engine.runner import CoveredCallRunner
from engine.scheduler import is_market_open
from engine.state import StateStore
from reporting import dashboard, metrics


def build_feed(cfg) -> FeedRouter:
    makers = {
        "ibkr": lambda: IBKRFeed(cfg.ibkr.host, cfg.ibkr.port, cfg.ibkr.client_id),
        "alpaca": lambda: AlpacaFeed(cfg.alpaca.key_id, cfg.alpaca.secret_key),
    }
    feeds = [makers[name]() for name in cfg.data_feed_order if name in makers]
    return FeedRouter(feeds)


def cmd_verify(cfg, feed: FeedRouter, broker: AlpacaPaperBroker) -> None:
    print("\n── Connection / entitlement check (read-only) ──")
    lvl = broker.options_trading_level()
    print(f"  Alpaca paper options trading level: {lvl} "
          f"({'OK for covered calls' if lvl >= 1 else 'NOT enabled, enable in Alpaca dashboard'})")
    acct = broker.get_account()
    print(f"  Alpaca paper cash: ${acct.cash:,.2f}  equity: ${acct.equity:,.2f}")
    # per-feed chain availability (truthful about which source actually serves)
    for f in feed._feeds:
        if not f.is_available():
            print(f"  feed {f.name}: not connected")
            continue
        try:
            n = len(f.get_call_chain(cfg.underlying, cfg.dte_min, cfg.dte_max, date.today()))
            print(f"  feed {f.name}: {n} calls in DTE[{cfg.dte_min},{cfg.dte_max}]")
        except Exception as e:
            print(f"  feed {f.name}: chain error ({e})")
    try:
        spot = feed.get_spot(cfg.underlying)
        chain = feed.get_call_chain(cfg.underlying, cfg.dte_min, cfg.dte_max, date.today())
        print(f"  router spot {cfg.underlying}: {spot:.2f}  -> {len(chain)} calls used")
        if chain:
            sample = sorted(chain, key=lambda q: q.strike)[len(chain) // 2]
            print(f"  sample: K={sample.strike} bid={sample.bid} ask={sample.ask} "
                  f"delta={sample.delta} iv={sample.iv} (greeks None => BS implied-vol fallback)")
    except Exception as e:
        print(f"  chain check failed: {e}")
    print("────────────────────────────────────────────────\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--verify", action="store_true", help="read-only connection check")
    ap.add_argument("--no-orders", action="store_true", help="decide + report, submit nothing")
    ap.add_argument("--force-orders", action="store_true", help="submit even if market closed")
    ap.add_argument("--asof", default=None, help="ISO date override (testing)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config(args.config)        # runs the paper-only safety gate
    feed = build_feed(cfg)
    broker = AlpacaPaperBroker(cfg.alpaca.key_id, cfg.alpaca.secret_key)
    feed.connect()
    broker.connect()
    try:
        if args.verify:
            cmd_verify(cfg, feed, broker)
            return

        store = StateStore(cfg.db_path, cfg.starting_capital)
        runner = CoveredCallRunner(cfg, feed, broker, store)
        asof = date.fromisoformat(args.asof) if args.asof else date.today()

        if args.no_orders:
            allow = False
        elif args.force_orders:
            allow = True
        else:
            allow = is_market_open()
            if not allow:
                logging.warning("Market closed, running decisions but submitting no orders "
                                "(use --force-orders to override).")
        result = runner.run_once(asof=asof, allow_orders=allow)

        st = store.load()
        s = metrics.summarize(store, st, result.spot, cfg.starting_capital, cfg.underlying, asof)
        dashboard.render_console(s)
        dashboard.append_csv(str(cfg.project_dir / cfg.report_csv), s)
        store.close()
    finally:
        broker.disconnect()
        feed.disconnect()


if __name__ == "__main__":
    main()
