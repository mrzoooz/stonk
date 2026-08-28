"""Synthetic price-series builders used by the tests.

These construct textbook shapes (a Stage 2 advance, a three-T VCP base) so the
detectors can be checked against a pattern whose answer we already know.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _bars(closes, vols, wiggle=0.004, seed=0):
    rng = np.random.default_rng(seed)
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    opens = np.empty(n)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    span = closes * wiggle
    highs = np.maximum(opens, closes) + rng.uniform(0, 1, n) * span
    lows = np.minimum(opens, closes) - rng.uniform(0, 1, n) * span
    idx = pd.bdate_range(end="2026-08-28", periods=n)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes,
         "volume": np.asarray(vols, dtype=float)},
        index=idx,
    )


def leg(start: float, end: float, n: int) -> list[float]:
    """A smooth price leg from `start` to `end` over `n` bars."""
    return list(np.linspace(start, end, n, endpoint=False))


def stage2_vcp(seed: int = 1) -> pd.DataFrame:
    """A Stage 2 advance followed by a clean T1=20% / T2=10% / T3=6% VCP."""
    closes: list[float] = []
    vols: list[float] = []

    def push(vals, v):
        closes.extend(vals)
        vols.extend([v] * len(vals))

    # Long base then a sustained advance so the 150/200MAs rise and stack.
    push(leg(20, 24, 120), 1_000_000)
    push(leg(24, 60, 200), 1_400_000)      # the Stage 2 thrust
    # T1: 20% pullback on heavy volume, then recovery.
    push(leg(60, 48, 18), 2_600_000)
    push(leg(48, 58, 15), 1_500_000)
    # T2: 10% pullback on medium volume, then recovery.
    push(leg(58, 52.2, 12), 1_500_000)
    push(leg(52.2, 57.5, 10), 1_100_000)
    # T3: 6% pullback on very light volume, then a drift back toward the pivot.
    push(leg(57.5, 54.05, 8), 700_000)
    push(leg(54.05, 56.8, 7), 520_000)
    closes.append(56.9)
    vols.append(500_000)
    return _bars(closes, vols, seed=seed)


def benchmark(n: int, seed: int = 3) -> pd.Series:
    """A benchmark that grinds up slowly - a Stage 2 name should beat it."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0003, 0.008, n)
    closes = 400 * np.exp(np.cumsum(steps))
    idx = pd.bdate_range(end="2026-08-28", periods=n)
    return pd.Series(closes, index=idx, name="close")


def benchmark_like(stock_close: pd.Series, share: float = 0.5, noise: float = 0.002,
                   seed: int = 11) -> pd.Series:
    """A benchmark whose returns are a damped version of the stock's.

    Gives the stock a beta of roughly 1/share against it, so tests can exercise
    the beta filter without hand-tuning numbers.
    """
    rng = np.random.default_rng(seed)
    rets = stock_close.pct_change().fillna(0.0).to_numpy()
    bench_rets = share * rets + rng.normal(0, noise, len(rets))
    closes = 400 * np.cumprod(1 + bench_rets)
    return pd.Series(closes, index=stock_close.index, name="close")
