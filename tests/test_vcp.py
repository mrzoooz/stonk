import pytest

from screener import vcp
from screener.config import load_config
from tests.synth import _bars, leg, stage2_vcp

CFG = load_config()


def build_base(t1_low=48.0, t2_low=52.2, t3_low=55.3, tail=56.9, last=57.0, seed=2):
    """A Stage 2 advance followed by three contractions with the given lows."""
    closes, vols = [], []

    def push(vals, vol):
        closes.extend(vals)
        vols.extend([vol] * len(vals))

    push(leg(20, 24, 120), 1_000_000)
    push(leg(24, 60, 200), 1_400_000)
    push(leg(60, t1_low, 18), 2_600_000)
    push(leg(t1_low, 58, 15), 1_500_000)
    push(leg(58, t2_low, 12), 1_500_000)
    push(leg(t2_low, 57.5, 10), 1_100_000)
    push(leg(57.5, t3_low, 8), 700_000)
    push(leg(t3_low, tail, 7), 520_000)
    closes.append(last)
    vols.append(500_000)
    return _bars(closes, vols, wiggle=0.002, seed=seed)


def test_finds_three_shrinking_contractions():
    res = vcp.detect(build_base(), CFG)
    depths = [c.depth_pct for c in res.contractions]
    assert len(depths) == 3
    assert depths[0] > depths[1] > depths[2]
    assert depths[0] == pytest.approx(20, abs=2)
    assert depths[1] == pytest.approx(10, abs=2)
    assert depths[2] == pytest.approx(4, abs=1.5)


def test_volume_falls_across_contractions():
    res = vcp.detect(build_base(), CFG)
    vols = [c.avg_volume for c in res.contractions]
    assert vols[0] > vols[1] > vols[2]


def test_lows_are_non_decreasing_by_construction():
    res = vcp.detect(build_base(), CFG)
    lows = [c.low for c in res.contractions]
    assert lows == sorted(lows)


def test_highs_are_non_increasing_by_construction():
    res = vcp.detect(build_base(), CFG)
    highs = [c.high for c in res.contractions]
    assert highs == sorted(highs, reverse=True)


def test_pivot_and_support_come_from_the_final_contraction():
    res = vcp.detect(build_base(), CFG)
    final = res.contractions[-1]
    assert res.metrics["pivot"] == pytest.approx(final.high, abs=1e-3)
    assert res.metrics["support"] == pytest.approx(final.low, abs=1e-3)


def test_risk_is_pivot_relative_to_support():
    res = vcp.detect(build_base(), CFG)
    m = res.metrics
    expected = (m["pivot"] - m["support"]) / m["pivot"] * 100
    assert m["risk_pct"] == pytest.approx(expected, abs=0.01)
    assert m["risk_pct"] == pytest.approx(m["final_depth_pct"], abs=0.01)


def test_tight_base_passes_and_wide_base_fails_the_risk_ceiling():
    tight = vcp.detect(build_base(t3_low=55.3), CFG)
    assert tight.passed, tight.reason
    assert tight.metrics["risk_pct"] <= CFG["risk"]["max_risk_pct"]

    wide = vcp.detect(build_base(t3_low=52.5, seed=5), CFG)
    assert not wide.passed
    assert "risk" in wide.reason


def test_a_broken_support_is_not_a_finished_consolidation():
    """If price undercuts the last low, that low was only a midpoint."""
    res = vcp.detect(build_base(t3_low=55.3, tail=54.0, last=53.0), CFG)
    assert not res.passed


def test_status_flags_a_breakout_above_the_pivot():
    res = vcp.detect(build_base(tail=57.4, last=58.2), CFG)
    assert res.status.startswith("breakout")
    assert res.metrics["distance_to_pivot_pct"] > 0


def test_status_flags_an_extended_stock():
    res = vcp.detect(build_base(tail=58.0, last=70.0), CFG)
    assert res.status == "extended"
    assert not res.passed


def test_no_base_when_price_only_goes_straight_up():
    closes = leg(20, 120, 400)
    df = _bars(closes, [1_000_000] * len(closes), wiggle=0.001, seed=4)
    res = vcp.detect(df, CFG)
    assert not res.passed


def test_min_contractions_is_enforced():
    cfg = load_config()
    cfg["vcp"]["min_contractions"] = 5
    res = vcp.detect(build_base(), cfg)
    assert not res.passed
    assert "contraction" in res.reason


def test_short_history_is_rejected_cleanly():
    df = _bars(leg(10, 12, 20), [1_000] * 20)
    res = vcp.detect(df, CFG)
    assert not res.passed
    assert res.contractions == []
