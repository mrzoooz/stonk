import numpy as np
import pandas as pd
import pytest

from screener import indicators as ind
from tests.synth import _bars, benchmark_like, leg, stage2_vcp


def test_sma_matches_rolling_mean():
    s = pd.Series(range(10), dtype=float)
    assert ind.sma(s, 3).iloc[-1] == pytest.approx((7 + 8 + 9) / 3)
    # Not enough history yields NaN rather than a partial average.
    assert np.isnan(ind.sma(s, 3).iloc[1])


def test_slope_pct_sign():
    rising = pd.Series(np.linspace(10, 12, 50))
    falling = pd.Series(np.linspace(12, 10, 50))
    assert ind.slope_pct(rising, 20) > 0
    assert ind.slope_pct(falling, 20) < 0
    assert np.isnan(ind.slope_pct(pd.Series([1.0, 2.0]), 20))


def test_weekly_candles_counted_by_open_close():
    df = stage2_vcp()
    up, down = ind.up_down_weeks(df, 52)
    assert up + down <= 52
    assert up > down  # the synthetic name is in a sustained advance


def test_beta_of_a_series_against_itself_is_one():
    df = stage2_vcp()
    assert ind.beta(df["close"], df["close"], 252) == pytest.approx(1.0, abs=1e-9)


def test_beta_scales_with_damped_benchmark():
    df = stage2_vcp()
    bench = benchmark_like(df["close"], share=0.5, noise=0.0)
    # Benchmark moves half as much, so the stock's beta against it is ~2.
    assert ind.beta(df["close"], bench, 252) == pytest.approx(2.0, rel=0.1)


def test_dollar_volume_sum_vs_mean():
    df = _bars(leg(10, 11, 30), [1_000_000] * 30)
    total = ind.dollar_volume(df, 21, "sum")
    mean = ind.dollar_volume(df, 21, "mean")
    assert total == pytest.approx(mean * 21)
