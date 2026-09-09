"""Split back-adjustment: it must fix real splits and leave real moves alone."""
import numpy as np
import pandas as pd
import pytest

from screener import adjust, stage2
from screener.config import load_config
from tests.test_vcp import build_base

CFG = load_config()


def apply_unadjusted_split(df, sessions_ago, factor=2.0, volume=True):
    """Undo a split in the data, i.e. put the series back the way a raw feed
    reports it: older bars at the pre-split scale."""
    out = df.copy()
    cut = out.index[len(out) - sessions_ago]
    older = out.index < cut
    for col in ("open", "high", "low", "close"):
        out.loc[older, col] = out.loc[older, col] * factor
    if volume:
        out.loc[older, "volume"] = out.loc[older, "volume"] / factor
    return out


def test_detects_a_forward_split():
    raw = apply_unadjusted_split(build_base(), 100, 2.0)
    found = adjust.detect_splits(raw)
    assert len(found) == 1
    assert found[0].factor == pytest.approx(2.0)


def test_detects_a_reverse_split():
    raw = apply_unadjusted_split(build_base(), 100, 1 / 5)
    found = adjust.detect_splits(raw)
    assert len(found) == 1
    assert found[0].factor == pytest.approx(0.2)


def test_back_adjustment_restores_the_original_series():
    clean = build_base()
    raw = apply_unadjusted_split(clean, 100, 2.0)
    fixed, adjustments = adjust.back_adjust(raw)
    assert len(adjustments) == 1
    np.testing.assert_allclose(
        fixed["close"].to_numpy(), clean["close"].to_numpy(), rtol=1e-9
    )
    np.testing.assert_allclose(
        fixed["volume"].to_numpy(), clean["volume"].to_numpy(), rtol=1e-9
    )


def test_back_adjustment_repairs_the_stage2_checks_a_split_broke():
    clean = build_base()
    raw = apply_unadjusted_split(clean, 100, 2.0)

    def checks(df):
        return {c.key: c.passed for c in stage2.evaluate(df, CFG, None).checks}

    good, broken = checks(clean), checks(raw)
    damaged = [k for k in good if good[k] and not broken[k]]
    assert damaged, "the fixture should break checks, or it is not testing anything"

    repaired = checks(adjust.back_adjust(raw)[0])
    assert all(repaired[k] for k in damaged)


def test_a_genuine_crash_is_not_treated_as_a_split():
    """A real halving on a volume spike must be left alone."""
    df = build_base().copy()
    i = len(df) - 100
    col_c = df.columns.get_loc("close")
    col_v = df.columns.get_loc("volume")
    # Halve the price from this bar onward, on a huge volume day.
    for col in ("open", "high", "low", "close"):
        df.iloc[i:, df.columns.get_loc(col)] = df.iloc[i:, df.columns.get_loc(col)] * 0.5
    df.iloc[i, col_v] = df["volume"].iloc[:i].median() * 30
    assert adjust.detect_splits(df) == []


def test_a_large_move_without_a_volume_step_is_not_a_split():
    df = build_base().copy()
    i = len(df) - 100
    for col in ("open", "high", "low", "close"):
        df.iloc[i:, df.columns.get_loc(col)] = df.iloc[i:, df.columns.get_loc(col)] * 0.5
    # Volume unchanged across the step - not what a split looks like.
    assert adjust.detect_splits(df) == []


def test_clean_history_is_returned_untouched():
    clean = build_base()
    fixed, adjustments = adjust.back_adjust(clean)
    assert adjustments == []
    assert fixed is clean
