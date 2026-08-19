"""
Black-Scholes call pricing, delta, and prob-ITM. Pure stdlib (math.erf) so the
strategy core has zero numeric/network dependencies and is trivially testable.

Conventions:
  S     spot price of the underlying
  K     strike
  T     time to expiry in YEARS (use days/365)
  r     continuously-compounded risk-free rate (annual)
  sigma annualized implied volatility
  q     continuous dividend yield (annual)

prob-ITM is the risk-neutral probability the call finishes in the money,
P(S_T > K) = N(d2). Delta is exp(-qT)*N(d1). For an OUT-OF-THE-MONEY call both
rise monotonically toward 1 as the strike falls toward (and below) spot, and as
T and sigma increase, see tests/test_greeks.py.
"""
from __future__ import annotations

import math

SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float, q: float) -> tuple[float, float]:
    vt = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vt
    d2 = d1 - vt
    return d1, d2


def bs_call_price(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0) -> float:
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)  # intrinsic at/after expiry, or with no vol
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    return S * math.exp(-q * T) * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)


def bs_call_delta(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0) -> float:
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    d1, _ = _d1_d2(S, K, T, r, sigma, q)
    return math.exp(-q * T) * norm_cdf(d1)


def prob_itm_call(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0) -> float:
    """Risk-neutral P(S_T > K) = N(d2)."""
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    _, d2 = _d1_d2(S, K, T, r, sigma, q)
    return norm_cdf(d2)


def implied_vol_call(
    price: float, S: float, K: float, T: float, r: float, q: float = 0.0,
    lo: float = 1e-4, hi: float = 5.0, tol: float = 1e-6, max_iter: int = 100,
) -> float | None:
    """
    Recover sigma from a call mid price by bisection. Returns None if the price
    is outside the no-arbitrage band (can't be matched by any sigma).
    """
    if T <= 0:
        return None
    intrinsic = max(S * math.exp(-q * T) - K * math.exp(-r * T), 0.0)
    upper_bound = S * math.exp(-q * T)
    if price < intrinsic - tol or price > upper_bound + tol:
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        diff = bs_call_price(S, K, T, r, mid, q) - price
        if abs(diff) < tol:
            return mid
        if diff > 0:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)
