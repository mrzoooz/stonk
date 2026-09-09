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


def test_a_stock_that_cleared_its_pivot_is_a_missed_entry_not_a_candidate():
    """The entry is the break above the pivot; a nightly screen sees it after
    the fact, so a stock already through it has to be excluded."""
    res = vcp.detect(build_base(tail=57.4, last=58.2), CFG)
    assert res.status == "broke_out"
    assert not res.passed
    assert "pivot already broken" in res.reason
    assert res.metrics["pivot_broken"] is True


def test_an_intraday_pierce_counts_even_if_it_closed_back_below():
    """A bar that traded through the pivot took the entry with it."""
    import pandas as pd

    df = build_base(t3_low=55.3)
    base = vcp.detect(df, CFG)
    assert base.passed, base.reason
    pivot = base.metrics["pivot"]

    poked = df.copy()
    idx = poked.index[-1] + pd.Timedelta(days=1)
    # High clears the pivot, close settles under it.
    poked.loc[idx] = {"open": pivot * 0.99, "high": pivot * 1.01,
                      "low": pivot * 0.98, "close": pivot * 0.995,
                      "volume": float(poked["volume"].tail(50).mean())}
    res = vcp.detect(poked, CFG)
    assert res.metrics["distance_to_pivot_pct"] < 0, "closed below the pivot"
    assert res.status == "broke_out"
    assert not res.passed


def test_a_stock_still_under_its_pivot_stays_actionable():
    res = vcp.detect(build_base(t3_low=55.3), CFG)
    assert res.status == "actionable"
    assert res.metrics["pivot_broken"] is False
    assert res.metrics["high_since_support"] <= res.metrics["pivot"]
    assert res.passed, res.reason


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


def _append_breakout(df, pivot, days=2, vol_multiple=3.2):
    """Extend a base with a textbook breakout: through the pivot on big volume."""
    import numpy as np
    import pandas as pd

    avg50 = float(np.mean(df["volume"].to_numpy()[-50:]))
    out = df.copy()
    for step in range(1, days + 1):
        px = pivot * (1 + 0.012 * step)
        idx = out.index[-1] + pd.Timedelta(days=1)
        out.loc[idx] = {"open": px * 0.995, "high": px * 1.005, "low": px * 0.99,
                        "close": px, "volume": avg50 * vol_multiple}
    return out


def test_dryup_survives_the_breakout_it_is_supposed_to_precede():
    """A breakout's own volume surge must not read as "no dry-up".

    Measuring dry-up over the trailing sessions rejected valid patterns at the
    exact moment they triggered, because the breakout volume landed inside the
    measurement window.
    """
    base = build_base(t3_low=55.3)
    quiet = vcp.detect(base, CFG)
    assert quiet.passed, quiet.reason

    broken_out = vcp.detect(_append_breakout(base, quiet.metrics["pivot"]), CFG)
    # It is excluded for having already broken out - but never for dry-up,
    # which is the measurement this test is about.
    assert broken_out.status == "broke_out"
    assert "dry-up" not in broken_out.reason, broken_out.reason
    assert broken_out.metrics["volume_dryup_ratio"] < CFG["vcp"]["dryup_ratio"]


def test_dryup_is_stable_as_bars_are_added_after_the_pattern():
    """The reading describes the consolidation, so it must not drift with time."""
    base = build_base(t3_low=55.3)
    before = vcp.detect(base, CFG).metrics["volume_dryup_ratio"]
    after = vcp.detect(
        _append_breakout(base, vcp.detect(base, CFG).metrics["pivot"]), CFG
    ).metrics["volume_dryup_ratio"]
    assert before == pytest.approx(after, rel=0.02)


def test_a_noisy_final_contraction_still_fails_the_dryup_check():
    """The check must keep rejecting patterns whose final T is not quiet."""
    import numpy as np

    df = build_base(t3_low=55.3)
    res = vcp.detect(df, CFG)
    final = res.contractions[-1]
    loud = df.copy()
    col = loud.columns.get_loc("volume")
    loud.iloc[final.high_idx:final.low_idx + 1, col] *= 12
    out = vcp.detect(loud, CFG)
    assert not out.passed
    assert "dry-up" in out.reason


def test_recent_volume_is_reported_but_not_decisive():
    base = build_base(t3_low=55.3)
    res = vcp.detect(_append_breakout(base, vcp.detect(base, CFG).metrics["pivot"]), CFG)
    # The trailing window is loud during a breakout...
    assert res.metrics["recent_volume_vs_50d"] > 1.0
    # ...but the contraction itself was quiet, and that is what decides.
    assert res.metrics["volume_dryup_ratio"] < CFG["vcp"]["dryup_ratio"]
    assert "dry-up" not in res.reason


def test_a_long_base_keeps_its_most_recent_contractions():
    """Walking must reach the present.

    The walk used to halt at a fixed count, so a base with many cycles was
    abandoned part-way and the "final" contraction - the one that supplies the
    pivot - came from the middle of the base instead of the end.
    """
    import numpy as np
    from tests.synth import _bars

    closes, vols = [], []
    closes.extend(leg(20, 40, 150))
    vols.extend([1_000_000] * 150)
    # Fifteen shrinking swings, far more than the old cap of twelve.
    level, swing = 40.0, 6.0
    for _ in range(15):
        closes.extend(leg(level, level - swing, 7))
        closes.extend(leg(level - swing, level + 0.2, 7))
        vols.extend([900_000] * 14)
        level += 0.2
        swing *= 0.88
    df = _bars(closes, vols, wiggle=0.001, seed=11)

    res = vcp.detect(df, CFG)
    assert res.contractions, "a base this busy must yield contractions"
    final = res.contractions[-1]
    # The final contraction has to sit at the end of the series, not mid-base.
    assert final.low_idx > len(df) - 40, (
        f"final contraction at bar {final.low_idx} of {len(df)} - walk stopped early"
    )
    assert res.metrics["pivot"] == pytest.approx(final.high, abs=1e-3)


def build_stepped_base(seed=21):
    """A VCP whose consolidations step *upward*, as in the training example.

    Highs ascend 70 -> 72 -> 74 while the pauses tighten 6.7% -> 6% -> 3.8%.
    Each consolidation forms above the previous one, after breaking out of it.
    """
    closes, vols = [], []

    def push(vals, vol):
        closes.extend(vals)
        vols.extend([vol] * len(vals))

    push(leg(40, 66, 220), 1_200_000)          # the prior advance
    # 1st consolidation: 70 -> 65.31 is 6.7%
    push(leg(66, 70.0, 10), 1_500_000)
    push(leg(70.0, 65.31, 12), 2_400_000)
    push(leg(65.31, 72.0, 14), 1_600_000)
    # 2nd: 72 -> 67.68 is 6.0%, and its high is ABOVE the first's
    push(leg(72.0, 67.68, 11), 1_500_000)
    push(leg(67.68, 74.0, 12), 1_100_000)
    # 3rd: 74 -> 71.19 is 3.8%, higher again and tighter
    push(leg(74.0, 71.19, 9), 700_000)
    push(leg(71.19, 73.6, 8), 520_000)
    closes.append(73.7)
    vols.append(500_000)
    return _bars(closes, vols, wiggle=0.0015, seed=seed)


def test_a_stepped_vcp_is_detected_even_though_its_highs_ascend():
    """Contraction is about the depth of each pause, not a fixed ceiling.

    Anchoring on the window's highest high imposed descending highs and
    collapsed any upward-stepping base into a single consolidation.
    """
    res = vcp.detect(build_stepped_base(), CFG)
    depths = [c.depth_pct for c in res.contractions]
    highs = [c.high for c in res.contractions]

    assert len(depths) == 3, f"expected three consolidations, got {depths}"
    assert highs == sorted(highs), f"highs should ascend here, got {highs}"
    assert depths[0] > depths[1] > depths[2], f"depths must tighten, got {depths}"
    assert depths[0] == pytest.approx(6.7, abs=1.2)
    assert depths[1] == pytest.approx(6.0, abs=1.2)
    assert depths[2] == pytest.approx(3.8, abs=1.2)


def test_lows_still_ascend_in_a_stepped_base():
    """The rule that survives in both shapes: no low is ever taken out again."""
    res = vcp.detect(build_stepped_base(), CFG)
    lows = [c.low for c in res.contractions]
    assert lows == sorted(lows), lows


def test_the_pivot_of_a_stepped_base_is_the_final_high():
    res = vcp.detect(build_stepped_base(), CFG)
    final = res.contractions[-1]
    assert res.metrics["pivot"] == pytest.approx(final.high, abs=1e-3)
    # The pivot must be the most recent high, not the tallest earlier one.
    assert res.metrics["pivot"] == pytest.approx(max(c.high for c in res.contractions), abs=1e-3)


def build_base_with_broken_intermediate_low(seed=31):
    """One consolidation containing a low that gets undercut.

    From the training material's Amgen example: price falls to A, bounces,
    then breaks below A, and only later reaches B and holds. A is "a mid-decline
    rest", not the end of anything - the whole stretch is a single consolidation
    whose low is B.
    """
    closes, vols = [], []

    def push(vals, vol):
        closes.extend(vals)
        vols.extend([vol] * len(vals))

    push(leg(45, 70.0, 200), 1_200_000)
    # --- consolidation 1: A is undercut, B holds -------------------------
    push(leg(70.0, 66.0, 8), 2_200_000)      # down to A = 66
    push(leg(66.0, 68.2, 6), 1_800_000)      # bounce off A
    push(leg(68.2, 64.0, 7), 2_000_000)      # breaks below A - still the same period
    push(leg(64.0, 67.0, 6), 1_700_000)      # bounce
    push(leg(67.0, 63.0, 7), 1_900_000)      # down to B = 63
    push(leg(63.0, 72.0, 18), 1_500_000)     # rebounds, never revisits B
    # --- consolidation 2 --------------------------------------------------
    push(leg(72.0, 68.4, 9), 1_100_000)
    push(leg(68.4, 71.5, 10), 700_000)
    closes.append(71.6)
    vols.append(600_000)
    return _bars(closes, vols, wiggle=0.0012, seed=seed)


def test_a_low_that_is_undercut_does_not_end_a_consolidation():
    """A and B belong to one consolidation; only B, which held, closes it."""
    res = vcp.detect(build_base_with_broken_intermediate_low(), CFG)
    lows = [round(c.low, 1) for c in res.contractions]

    assert len(res.contractions) == 2, f"expected two consolidations, got {lows}"
    # B (~63), not A (~66), is the first consolidation's low.
    assert res.contractions[0].low == pytest.approx(63.0, abs=0.6), lows
    assert all(abs(low - 66.0) > 1.0 for low in lows), f"A leaked in as a low: {lows}"
    # And its high is the peak that started the whole stretch, not the bounce.
    assert res.contractions[0].high == pytest.approx(70.0, abs=0.6)


def test_the_second_consolidation_starts_after_the_first_low_held():
    res = vcp.detect(build_base_with_broken_intermediate_low(), CFG)
    first, second = res.contractions
    assert second.high_idx > first.low_idx, "a new period begins only after the last one closed"
    assert second.low > first.low, "lows must ascend"
    assert second.depth_pct < first.depth_pct, "and the pattern must tighten"


def test_a_base_that_has_barely_formed_is_rejected():
    """Criterion 4: the pattern should run months, not days.

    A base a few sessions long also produces a degenerate target, because the
    measured move projects the base's own depth and there is almost none.
    """
    import pandas as pd

    df = build_base(t3_low=55.3)
    # Keep only the tail: a base that starts a handful of bars ago.
    short = df.tail(280).copy()
    res = vcp.detect(short, CFG)
    if res.contractions:
        assert res.metrics["base_length_bars"] >= CFG["vcp"]["min_base_bars"] or not res.passed


def test_base_length_below_the_floor_fails_with_a_clear_reason():
    cfg = load_config()
    cfg["vcp"]["min_base_bars"] = 500          # nothing can satisfy this
    res = vcp.detect(build_base(t3_low=55.3), cfg)
    assert not res.passed
    assert "base only" in res.reason


def test_first_contraction_beyond_fifty_percent_is_rejected():
    """Criterion 2: past 50% the stock is still in stage 4, not basing."""
    cfg = load_config()
    cfg["vcp"]["max_first_depth_pct"] = 50.0
    deep = build_base(t1_low=28.0, t2_low=52.2, t3_low=55.3)
    res = vcp.detect(deep, cfg)
    if res.contractions and res.contractions[0].depth_pct > 50:
        assert not res.passed
        assert "stage 4 has not finished" in res.reason


def test_max_entry_keeps_the_risk_within_the_ceiling():
    """The entry ceiling is the stop plus the risk allowance, not the pivot."""
    res = vcp.detect(build_base(t3_low=55.3), CFG)
    m = res.metrics
    ceiling = CFG["risk"]["max_risk_pct"]
    assert m["max_entry"] == pytest.approx(m["support"] * (1 + ceiling / 100), abs=1e-3)
    assert m["max_entry"] > m["pivot"], "there should be room above the pivot to fill"
    # Buying at the ceiling still keeps the loss to the stop within the limit.
    risk_at_ceiling = (m["max_entry"] - m["support"]) / m["max_entry"] * 100
    assert risk_at_ceiling <= ceiling + 1e-6


def test_preferred_levels_are_reported_separately_from_pass_fail():
    res = vcp.detect(build_base(t3_low=55.3), CFG)
    pref = res.metrics["preferred"]
    assert set(pref) == {"contractions", "base_length", "first_depth", "final_depth", "shrink"}
    assert all(isinstance(x, bool) for x in pref.values())
    # This fixture tightens 20.2% -> 10.2% -> 4.2%, so its worst step is 0.506
    # - a hair over half, and therefore short of the ideal. Reported, not
    # enforced: the base still passes.
    assert res.metrics["worst_shrink_ratio"] == pytest.approx(0.506, abs=0.02)
    assert pref["shrink"] is False
    assert res.passed, res.reason


def test_a_base_that_truly_halves_each_time_meets_the_ideal():
    res = vcp.detect(build_base(t1_low=44.0, t2_low=53.5, t3_low=56.2), CFG)
    if len(res.contractions) >= 2:
        ratios = [
            res.contractions[i].depth_pct / res.contractions[i - 1].depth_pct
            for i in range(1, len(res.contractions))
        ]
        assert res.metrics["preferred"]["shrink"] is (max(ratios) <= 0.5)


def test_a_shallow_wobble_between_consolidations_is_skipped_not_a_wall():
    """Reproduces NTRA: a five-day dip sat between two real consolidations.

    Raw cycles were 13.89% (288 -> 248), then 6.45% (326 -> 305), then 7.34%
    (343 -> 318). Walking back from the last, 7.34% is wider than 6.45%, so
    selection stopped there and reported a single consolidation - discarding
    the 13.89% base entirely and, with it, most of the measured move.
    """
    from screener.vcp import Contraction, _select_run

    def cyc(i, high, low, vol, hi_idx, lo_idx):
        depth = (high - low) / high * 100
        return Contraction(index=i, high_idx=hi_idx, low_idx=lo_idx,
                           high_date="", low_date="", high=high, low=low,
                           depth_pct=depth, bars=lo_idx - hi_idx + 1, avg_volume=vol)

    cycles = [
        cyc(1, 225.70, 204.16, 3.0e6, 10, 20),
        cyc(2, 227.90, 208.79, 2.8e6, 22, 30),
        cyc(3, 288.04, 248.03, 2.5e6, 40, 60),   # the real T1
        cyc(4, 326.03, 305.00, 2.2e6, 70, 75),   # five-day wobble
        cyc(5, 343.18, 318.00, 2.0e6, 85, 90),   # the real T2
    ]
    cfg = load_config()["vcp"]
    run = _select_run(cycles, cfg)
    depths = [round(c.depth_pct, 2) for c in run]

    assert len(run) >= 2, f"the 13.89% consolidation must survive, got {depths}"
    assert any(abs(d - 13.89) < 0.1 for d in depths), depths
    assert depths[-1] == pytest.approx(7.34, abs=0.05), "the final T still sets the pivot"
    # And the run is a genuine contraction sequence.
    assert all(depths[i] <= depths[i - 1] for i in range(1, len(depths))), depths


def test_skipping_never_breaks_the_ascending_low_rule():
    """Skipping looks further back, so it must not pick up a lower structure."""
    res = vcp.detect(build_stepped_base(), CFG)
    lows = [c.low for c in res.contractions]
    assert lows == sorted(lows), lows
