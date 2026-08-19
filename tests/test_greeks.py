"""Black-Scholes sanity + the monotonicities the spec calls out."""
import math

from strategy import greeks

S, R, Q = 100.0, 0.04, 0.0
T = 30 / 365.0
VOL = 0.20


def test_ranges():
    for K in (80, 90, 100, 110, 120):
        d = greeks.bs_call_delta(S, K, T, R, VOL, Q)
        p = greeks.prob_itm_call(S, K, T, R, VOL, Q)
        assert 0.0 <= d <= 1.0
        assert 0.0 <= p <= 1.0


def test_intrinsic_bounds():
    # deep ITM ~ forward intrinsic; deep OTM ~ 0
    deep_itm = greeks.bs_call_price(S, 50, T, R, VOL, Q)
    assert deep_itm > (S - 50) * math.exp(-R * T) - 0.5
    assert greeks.bs_call_price(S, 200, T, R, VOL, Q) < 0.01


def test_expiry_degenerate():
    assert greeks.bs_call_price(S, 90, 0.0, R, VOL, Q) == 10.0
    assert greeks.bs_call_delta(S, 90, 0.0, R, VOL, Q) == 1.0
    assert greeks.bs_call_delta(S, 110, 0.0, R, VOL, Q) == 0.0
    assert greeks.prob_itm_call(S, 90, 0.0, R, VOL, Q) == 1.0
    assert greeks.prob_itm_call(S, 110, 0.0, R, VOL, Q) == 0.0


def test_monotonic_in_strike():
    """Both delta and prob-ITM strictly rise as the strike falls toward spot."""
    strikes = [120, 115, 110, 105, 100, 95, 90]  # decreasing
    deltas = [greeks.bs_call_delta(S, K, T, R, VOL, Q) for K in strikes]
    probs = [greeks.prob_itm_call(S, K, T, R, VOL, Q) for K in strikes]
    assert deltas == sorted(deltas)
    assert probs == sorted(probs)
    assert all(b > a for a, b in zip(deltas, deltas[1:]))
    assert all(b > a for a, b in zip(probs, probs[1:]))


def test_delta_monotonic_in_vol():
    """For an OTM call, delta (the cap proxy) strictly increases with vol."""
    K = 110.0
    vols = [0.05, 0.10, 0.20, 0.40, 0.80]
    deltas = [greeks.bs_call_delta(S, K, T, R, v, Q) for v in vols]
    assert all(b > a for a, b in zip(deltas, deltas[1:]))


def test_delta_monotonic_in_dte():
    """OTM call with q=0: delta strictly increases with time to expiry."""
    K = 110.0
    dtes = [7, 21, 45, 90, 180]
    deltas = [greeks.bs_call_delta(S, K, d / 365.0, R, VOL, 0.0) for d in dtes]
    assert all(b > a for a, b in zip(deltas, deltas[1:]))


def test_implied_vol_roundtrip():
    K = 105.0
    price = greeks.bs_call_price(S, K, T, R, 0.27, Q)
    iv = greeks.implied_vol_call(price, S, K, T, R, Q)
    assert iv is not None and abs(iv - 0.27) < 1e-3


def test_implied_vol_out_of_band():
    # a price above the underlying can't be matched by any sigma
    assert greeks.implied_vol_call(S + 5, S, 105.0, T, R, Q) is None
