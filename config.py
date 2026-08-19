"""
Configuration loading + the paper-only safety gate.

Config = YAML (strategy params) + environment (secrets). Loading runs the
safety validations eagerly, so an unsafe/misconfigured setup fails at import-
time of the config rather than mid-trading.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

PAPER_ENDPOINT = "https://paper-api.alpaca.markets"


class UnsafeConfigError(RuntimeError):
    """Raised when a safety invariant would be violated. Never caught in-loop."""


@dataclass(frozen=True)
class AlpacaCreds:
    key_id: str
    secret_key: str
    endpoint: str


@dataclass(frozen=True)
class IBKRConn:
    host: str
    port: int
    client_id: int


@dataclass(frozen=True)
class Config:
    # strategy / sizing
    underlying: str
    starting_capital: float
    contracts_per_cycle: int
    benchmark_mode: bool
    target_dte: int
    dte_tolerance: int
    otm_pct: float
    assignment_cap: float
    min_premium_dollars: float
    max_bid_ask_spread_pct: float
    roll_trigger_dte: int
    roll_if_itm: bool
    reinvest_income: bool
    risk_free_rate: float
    dividend_yield: float
    data_feed_order: tuple[str, ...]
    # safety
    i_understand_this_is_live: bool
    kill_switch_file: str
    # persistence
    db_path: str
    report_csv: str
    # secrets / connections
    alpaca: AlpacaCreds
    ibkr: IBKRConn
    project_dir: Path

    # ── derived helpers ──────────────────────────────────────────────────────
    @property
    def dte_min(self) -> int:
        return self.target_dte - self.dte_tolerance

    @property
    def dte_max(self) -> int:
        return self.target_dte + self.dte_tolerance

    @property
    def shares_per_cycle(self) -> int:
        return self.contracts_per_cycle * 100

    @property
    def kill_switch_path(self) -> Path:
        return self.project_dir / self.kill_switch_file

    def kill_switch_engaged(self) -> bool:
        return self.kill_switch_path.exists()

    # ── the safety gate ──────────────────────────────────────────────────────
    def validate(self) -> None:
        """Hard-fail on any paper-only violation. Called at load time."""
        if self.i_understand_this_is_live:
            raise UnsafeConfigError(
                "i_understand_this_is_live is true. This project is PAPER-ONLY; "
                "the live path is intentionally unreachable. Refusing to start."
            )
        if self.alpaca.endpoint.rstrip("/") != PAPER_ENDPOINT:
            raise UnsafeConfigError(
                f"Alpaca endpoint is {self.alpaca.endpoint!r}, not the paper URL "
                f"{PAPER_ENDPOINT!r}. Refusing to start."
            )
        # Alpaca paper keys are prefixed 'PK'; live keys 'AK'. Guard against a
        # live key being dropped in by mistake.
        if not self.alpaca.key_id.startswith("PK"):
            raise UnsafeConfigError(
                "ALPACA_API_KEY_ID does not have the paper 'PK' prefix. Refusing "
                "to start with what looks like a live key."
            )
        if self.contracts_per_cycle < 1:
            raise UnsafeConfigError("contracts_per_cycle must be >= 1")
        if not (0 < self.assignment_cap < 1):
            raise UnsafeConfigError("assignment_cap must be in (0, 1)")


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise UnsafeConfigError(f"Required environment variable {name} is unset.")
    return val


def load_config(
    yaml_path: str | os.PathLike = "config.yaml",
    project_dir: str | os.PathLike | None = None,
) -> Config:
    """Load YAML + .env, build Config, run safety validation."""
    yaml_path = Path(yaml_path)
    proj = Path(project_dir) if project_dir else yaml_path.resolve().parent
    load_dotenv(proj / ".env")  # no-op if absent; real secrets live here

    with open(yaml_path) as f:
        y = yaml.safe_load(f)

    alpaca = AlpacaCreds(
        key_id=_require_env("ALPACA_API_KEY_ID"),
        secret_key=_require_env("ALPACA_API_SECRET_KEY"),
        endpoint=os.environ.get("ALPACA_PAPER_ENDPOINT", PAPER_ENDPOINT),
    )
    ibkr = IBKRConn(
        host=os.environ.get("IBKR_HOST", "127.0.0.1"),
        port=int(os.environ.get("IBKR_PORT", "4002")),
        client_id=int(os.environ.get("IBKR_CLIENT_ID", "31")),
    )

    cfg = Config(
        underlying=str(y["underlying"]),
        starting_capital=float(y["starting_capital"]),
        contracts_per_cycle=int(y["contracts_per_cycle"]),
        benchmark_mode=bool(y["benchmark_mode"]),
        target_dte=int(y["target_dte"]),
        dte_tolerance=int(y["dte_tolerance"]),
        otm_pct=float(y["otm_pct"]),
        assignment_cap=float(y["assignment_cap"]),
        min_premium_dollars=float(y["min_premium_dollars"]),
        max_bid_ask_spread_pct=float(y["max_bid_ask_spread_pct"]),
        roll_trigger_dte=int(y["roll_trigger_dte"]),
        roll_if_itm=bool(y["roll_if_itm"]),
        reinvest_income=bool(y["reinvest_income"]),
        risk_free_rate=float(y["risk_free_rate"]),
        dividend_yield=float(y["dividend_yield"]),
        data_feed_order=tuple(y.get("data_feed_order", ["ibkr", "alpaca"])),
        i_understand_this_is_live=bool(y.get("i_understand_this_is_live", False)),
        kill_switch_file=str(y.get("kill_switch_file", "KILL_SWITCH")),
        db_path=str(y.get("db_path", "covered_call.sqlite")),
        report_csv=str(y.get("report_csv", "reports/cycles.csv")),
        alpaca=alpaca,
        ibkr=ibkr,
        project_dir=proj,
    )
    cfg.validate()
    return cfg
