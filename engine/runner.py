"""
Orchestration: one idempotent cycle = reconcile -> settle matured options +
dividends -> ensure share base -> manage/roll the short call -> write a new call
-> reinvest -> mark equity vs benchmark -> persist + report.

Feed/broker/store are injected, so the integration test drives the exact same
code path with mocks (zero network).
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass
from datetime import date

from broker.base import AssetClass, Broker, Order, OrderSide, OrderStatus
from data.base import DataFeed, OptionQuote
from engine import settlement
from engine.state import PortfolioState, ShortCallRecord, StateStore, reconcile
from strategy import covered_call as cc

logger = logging.getLogger("runner")


@dataclass
class RunResult:
    asof: str
    spot: float
    actions: list[str]
    strategy_equity: float
    benchmark_equity: float
    skipped_for_kill_switch: bool = False


class CoveredCallRunner:
    def __init__(self, cfg, feed: DataFeed, broker: Broker, store: StateStore,
                 fill_retries: int = 1, fill_wait_s: float = 2.0, fill_polls: int = 5):
        self.cfg = cfg
        self.feed = feed
        self.broker = broker
        self.store = store
        self.fill_retries = fill_retries
        self.fill_wait_s = fill_wait_s
        self.fill_polls = fill_polls

    # ── order helpers ────────────────────────────────────────────────────────
    def _await_fill(self, order: Order) -> Order:
        """Poll until filled or exhausted. MockBroker fills instantly."""
        for _ in range(self.fill_polls):
            if order.status == OrderStatus.FILLED:
                return order
            if order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED):
                return order
            _time.sleep(self.fill_wait_s)
            order = self.broker.get_order(order.id)
        return order

    def _limit(self, symbol, qty, side, price, asset_class) -> Order:
        logger.info("ORDER %s %s x%s @ %.2f (%s)", side.value, symbol, qty, price,
                    asset_class.value)
        o = self.broker.submit_limit_order(symbol, qty, side, price, asset_class)
        return self._await_fill(o)

    def _write_with_retry(self, sel: cc.StrikeSelection) -> Order | None:
        """
        Sell-to-open at mid first (best price); if it doesn't fill, fall back to a
        marketable limit just through the bid so the write reliably completes. The
        spread is already capped (§3 max_bid_ask_spread_pct), so hitting the bid on
        an eligible option gives up only a small, bounded amount.
        """
        q = sel.quote
        bid_px = max(round(q.bid - 0.01, 2), 0.01)
        prices = [sel.limit_price] + [bid_px] * self.fill_retries   # mid, then bid
        for attempt, px in enumerate(prices, 1):
            if not px or px <= 0:
                continue
            o = self._limit(q.occ_symbol, self.cfg.contracts_per_cycle, OrderSide.SELL,
                            px, AssetClass.OPTION)
            if o.status == OrderStatus.FILLED:
                return o
            logger.warning("write attempt %d @ %.2f unfilled (status=%s); %s", attempt, px,
                           o.status, "retrying at bid" if attempt <= self.fill_retries else "skipping")
            if o.status not in (OrderStatus.CANCELED, OrderStatus.REJECTED):
                try:
                    self.broker.cancel_order(o.id)
                except Exception:
                    pass
        return None

    # ── pieces of a cycle ────────────────────────────────────────────────────
    def _settle(self, st: PortfolioState, spot: float, asof: date, actions: list[str]) -> None:
        # dividends since last run (applied to strategy cash AND benchmark shares)
        start = date.fromisoformat(st.last_run_on) if st.last_run_on else asof
        divs = self.feed.get_dividends(self.cfg.underlying, start, asof)
        if divs and st.shares:
            dc = settlement.credit_dividends(st.shares, divs)
            if dc.cash:
                st.cash += dc.cash
                st.cum_dividends += dc.cash
                st.income_pool += dc.cash
                self.store.append_ledger(asof.isoformat(), "dividend", dc.cash,
                                         f"{st.shares} sh")
                actions.append(f"dividend +${dc.cash:.2f}")
        if divs and st.benchmark_initialized and spot > 0:
            bdc = settlement.credit_dividends(int(st.benchmark_shares), divs)
            if bdc.cash:  # benchmark = total-return: reinvest dividends
                add = bdc.cash / spot
                st.benchmark_shares += add
                actions.append(f"benchmark div reinvest +{add:.4f}sh")

        # matured short call -> deterministic expiry/assignment
        sc = st.short_call
        if sc and settlement.is_matured(sc, asof):
            res = settlement.settle_expiry(sc, spot, st.cost_basis)
            st.cash += res.cash_delta
            st.shares += res.shares_delta
            st.short_call = None
            if res.assigned:
                st.assignment_count += 1
                st.realized_share_pnl += res.realized_share_pnl
                self.store.append_ledger(asof.isoformat(), "assignment", res.cash_delta, res.note)
            actions.append(res.note)
            logger.info("SETTLE: %s", res.note)

    def _ensure_base(self, st: PortfolioState, spot: float, asof: date,
                     actions: list[str], allow_orders: bool) -> None:
        need = self.cfg.shares_per_cycle - st.shares
        if need <= 0:
            return
        if not allow_orders:
            actions.append(f"would buy {need} sh (orders disabled)")
            return
        # bounded marketable limit (0.1% through) so the underlying leg actually
        # fills on a liquid ETF, still a limit order, never a market order.
        price = round(spot * 1.001, 2)
        o = self._limit(self.cfg.underlying, need, OrderSide.BUY, price, AssetClass.EQUITY)
        if o.status == OrderStatus.FILLED:
            fill = o.filled_avg_price or price
            new_total = st.shares + int(o.filled_qty)
            st.cost_basis = ((st.shares * st.cost_basis + int(o.filled_qty) * fill)
                             / new_total) if new_total else 0.0
            st.shares = new_total
            st.cash -= fill * int(o.filled_qty)
            self.store.append_ledger(asof.isoformat(), "buy_shares", -fill * int(o.filled_qty),
                                     f"{int(o.filled_qty)} sh @ {fill:.2f}")
            actions.append(f"bought {int(o.filled_qty)} sh @ {fill:.2f}")

    def _manage_short(self, st: PortfolioState, spot: float, asof: date,
                      actions: list[str], allow_orders: bool) -> bool:
        """Returns True if we may write a new call afterward."""
        sc = st.short_call
        if sc is None:
            return True
        decision = cc.roll_decision(_view(sc), spot, self.cfg, asof)
        actions.append(f"{decision.type.value}: {decision.reason}")
        logger.info("ROLL %s: %s", decision.type.value, decision.reason)
        if decision.type == cc.ActionType.HOLD:
            return False
        if decision.type == cc.ActionType.ALLOW_ASSIGNMENT:
            return False  # wait for expiry settlement; do not write
        # LET_EXPIRE or ROLL_UP_OUT -> buy-to-close, then allow a rewrite
        if not allow_orders:
            actions.append("would buy-to-close (orders disabled)")
            return False
        mark = self._option_mark(sc, spot)
        o = self._limit(sc.occ_symbol, sc.qty, OrderSide.BUY, max(round(mark, 2), 0.01),
                        AssetClass.OPTION)
        if o.status == OrderStatus.FILLED:
            cost = o.filled_avg_price * 100 * sc.qty
            st.cash -= cost
            st.cum_btc_cost += cost
            st.income_pool -= cost
            self.store.append_ledger(asof.isoformat(), "buy_to_close", -cost, sc.occ_symbol)
            actions.append(f"bought-to-close {sc.occ_symbol} @ {o.filled_avg_price:.2f}")
            st.short_call = None
            return True
        actions.append("buy-to-close unfilled; keeping short call")
        return False

    def _write(self, st: PortfolioState, spot: float, chain: list[OptionQuote],
               asof: date, actions: list[str], allow_orders: bool) -> None:
        sel = cc.select_call(spot, chain, self.cfg, asof)
        self.store.append_cycle(
            ts=asof.isoformat(), cycle_index=st.cycle_index,
            action="select_call", reason=sel.reason, spot=spot,
            strike=sel.quote.strike if sel.quote else None,
            expiry=sel.quote.expiry.isoformat() if sel.quote else None,
            prob_itm=sel.prob_itm,
            premium=(sel.quote.mid if sel.quote else None),
            spread_pct=(sel.quote.spread_pct if sel.quote else None),
            order_id=None, detail=None,
        )
        if not sel.ok:
            actions.append(f"skip write: {sel.reason}")
            logger.info("SKIP WRITE: %s", sel.reason)
            return
        if not allow_orders:
            actions.append(f"would write {sel.quote.occ_symbol} @ {sel.limit_price}")
            return
        o = self._write_with_retry(sel)
        if o and o.status == OrderStatus.FILLED:
            credit = o.filled_avg_price * 100 * self.cfg.contracts_per_cycle
            st.cash += credit
            st.cum_premium += credit
            st.income_pool += credit
            st.short_call = ShortCallRecord(
                occ_symbol=sel.quote.occ_symbol, strike=sel.quote.strike,
                expiry=sel.quote.expiry.isoformat(), entry_premium=o.filled_avg_price,
                qty=self.cfg.contracts_per_cycle, opened_on=asof.isoformat())
            self.store.append_ledger(asof.isoformat(), "premium", credit, sel.quote.occ_symbol)
            actions.append(f"WROTE {sel.quote.occ_symbol} K={sel.quote.strike} "
                           f"@ {o.filled_avg_price:.2f} (+${credit:.2f}, pITM={sel.prob_itm:.2f})")
            logger.info("WROTE %s @ %.2f", sel.quote.occ_symbol, o.filled_avg_price)
        else:
            actions.append("write unfilled after retry; skipped")

    def _reinvest(self, st: PortfolioState, spot: float, asof: date,
                  actions: list[str], allow_orders: bool) -> None:
        # reinvest ONLY accumulated income (premium + dividends - roll costs),
        # never the reserve capital, keeps the base at contracts*100 shares.
        n = cc.shares_to_reinvest(st.income_pool, spot, self.cfg)
        if n <= 0 or not allow_orders:
            return
        price = round(spot * 1.001, 2)   # bounded marketable limit (see _ensure_base)
        o = self._limit(self.cfg.underlying, n, OrderSide.BUY, price, AssetClass.EQUITY)
        if o.status == OrderStatus.FILLED:
            fill = o.filled_avg_price or price
            spent = fill * int(o.filled_qty)
            new_total = st.shares + int(o.filled_qty)
            st.cost_basis = (st.shares * st.cost_basis + spent) / new_total
            st.shares = new_total
            st.cash -= spent
            st.income_pool -= spent
            self.store.append_ledger(asof.isoformat(), "reinvest", -spent, f"{int(o.filled_qty)} sh")
            actions.append(f"reinvested income into {int(o.filled_qty)} sh")

    # ── marks / equity ───────────────────────────────────────────────────────
    def _option_mark(self, sc: ShortCallRecord, spot: float) -> float:
        """Real current price of the short call from the feed (includes time
        value); fall back to intrinsic only if the feed can't price it."""
        mid = self.feed.get_option_mid(self.cfg.underlying, sc.expiry_date(), sc.strike, "C")
        if mid is not None and mid >= 0:
            return mid
        return max(spot - sc.strike, 0.0)

    def _equity(self, st: PortfolioState, spot: float) -> tuple[float, float]:
        liab = 0.0
        if st.short_call:
            liab = self._option_mark(st.short_call, spot) * 100 * st.short_call.qty
        strat = st.cash + st.shares * spot - liab
        bench = st.benchmark_shares * spot + st.benchmark_cash
        return strat, bench

    # ── the cycle ────────────────────────────────────────────────────────────
    def run_once(self, asof: date | None = None, allow_orders: bool = True) -> RunResult:
        cfg = self.cfg
        asof = asof or date.today()
        st = self.store.load()
        st.cycle_index += 1
        actions: list[str] = []

        reconcile(st, self.broker, cfg.underlying)
        spot = self.feed.get_spot(cfg.underlying)
        logger.info("=== cycle %d  %s  %s spot=%.2f ===", st.cycle_index, asof,
                    cfg.underlying, spot)

        if not st.benchmark_initialized:
            sh = int(self.cfg.starting_capital // spot)
            st.benchmark_shares = sh
            st.benchmark_cash = self.cfg.starting_capital - sh * spot
            st.benchmark_initialized = True
            actions.append(f"benchmark init: {sh} sh + ${st.benchmark_cash:.2f}")

        self._settle(st, spot, asof, actions)

        kill = cfg.kill_switch_engaged()
        if kill:
            logger.warning("KILL SWITCH engaged (%s), reporting only, no new orders",
                           cfg.kill_switch_path)

        allow = allow_orders and not kill
        self._ensure_base(st, spot, asof, actions, allow)
        may_write = self._manage_short(st, spot, asof, actions, allow)
        if may_write and st.short_call is None and st.shares >= cfg.shares_per_cycle:
            chain = self.feed.get_call_chain(cfg.underlying, cfg.dte_min, cfg.dte_max, asof)
            self._write(st, spot, chain, asof, actions, allow)
        self._reinvest(st, spot, asof, actions, allow)

        strat_eq, bench_eq = self._equity(st, spot)
        st.last_run_on = asof.isoformat()
        self.store.append_equity(asof.isoformat(), strat_eq, bench_eq)
        self.store.append_cycle(
            ts=asof.isoformat(), cycle_index=st.cycle_index, action="cycle_summary",
            reason="; ".join(actions) or "no-op", spot=spot, strike=None, expiry=None,
            prob_itm=None, premium=None, spread_pct=None, order_id=None,
            detail=f"strat={strat_eq:.2f} bench={bench_eq:.2f}")
        self.store.save(st)

        return RunResult(asof.isoformat(), spot, actions, strat_eq, bench_eq,
                         skipped_for_kill_switch=kill)


def _view(sc: ShortCallRecord) -> cc.ShortCallView:
    return cc.ShortCallView(sc.occ_symbol, sc.strike, sc.expiry_date(), sc.entry_premium)
