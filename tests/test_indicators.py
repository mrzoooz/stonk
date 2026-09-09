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


def test_rs_score_weights_the_most_recent_quarter_double():
    """Two stocks up the same over a year rank differently by when they moved."""
    import numpy as np
    import pandas as pd
    from screener import indicators as ind

    idx = pd.date_range("2024-01-01", periods=300, freq="B")
    # Recent mover: flat, then a late run. Early mover: the run, then flat.
    late = np.concatenate([np.full(237, 100.0), np.linspace(100, 150, 63)])
    early = np.concatenate([np.linspace(100, 150, 63), np.full(237, 150.0)])
    a = ind.rs_score(pd.Series(late, index=idx))
    b = ind.rs_score(pd.Series(early, index=idx))
    assert a > b, (a, b)


def test_rs_score_needs_a_full_year():
    import pandas as pd
    import numpy as np
    from screener import indicators as ind

    short = pd.Series(np.linspace(10, 20, 200),
                      index=pd.date_range("2025-01-01", periods=200, freq="B"))
    assert not np.isfinite(ind.rs_score(short))


def test_rs_ratings_rank_the_whole_field_onto_1_to_99():
    from screener import indicators as ind

    scores = {f"S{i}": float(i) for i in range(100)}
    out = ind.rs_ratings(scores)
    assert out["S99"] == 99 and out["S0"] == 1
    # Monotone: a better raw score never gets a worse rating.
    ordered = [out[f"S{i}"] for i in range(100)]
    assert ordered == sorted(ordered)
    # Symbols with no score are simply absent rather than ranked as weakest.
    assert ind.rs_ratings({"a": float("nan")}) == {}
