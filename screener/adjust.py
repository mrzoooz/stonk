"""Back-adjust unadjusted price history for stock splits.

Nasdaq's endpoint serves `Close/Last`, which is not adjusted for corporate
actions. A split therefore leaves a step change in the series: every bar before
it sits at the old scale. That corrupts the moving averages for as long as the
split stays inside their window (a split 100 sessions ago moves a 200-day
average by about 45%), reads as an enormous fake contraction, and wrecks beta.

A split is distinguished from a genuine large price move by two things
together:

  * the close-to-close ratio sits very near a whole split factor, and
  * share volume steps the opposite way by about the same factor, because the
    volume feed is unadjusted too - a k-for-1 split roughly multiplies share
    count by k.

A real crash or squeeze moves price without that volume relationship, and its
event-day volume is an extreme outlier, which a split's is not. All three
conditions must hold before any history is rescaled.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# Whole-number split factors. 1.25/1.33/1.5/2.5 are deliberately absent: they
# sit inside the range of ordinary single-day moves in volatile small caps.
SPLIT_FACTORS = (2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 50)

PRICE_TOL = 0.015        # how near the close ratio must be to a whole factor
VOLUME_TOL = 0.45        # how near the volume step must be to that same factor
EVENT_SPIKE_MAX = 4.0    # a split does not come with a huge volume spike
MIN_MOVE_PCT = 35.0      # only inspect genuinely large single-day moves
WINDOW = 10              # sessions either side used for the volume medians


@dataclass
class Adjustment:
    date: str
    factor: float        # >1 forward split (price divided), <1 reverse split
    price_ratio: float
    volume_ratio: float


def _match_factor(ratio: float) -> float | None:
    for f in SPLIT_FACTORS:
        for cand in (float(f), 1.0 / f):
            if abs(ratio - cand) / cand <= PRICE_TOL:
                return cand
    return None


def detect_splits(df: pd.DataFrame) -> list[Adjustment]:
    """Find split-shaped discontinuities, newest last."""
    if len(df) < 3 * WINDOW:
        return []
    close = df["close"].to_numpy(dtype=float)
    vol = df["volume"].to_numpy(dtype=float)
    out: list[Adjustment] = []

    with np.errstate(divide="ignore", invalid="ignore"):
        move = np.abs(close[1:] / close[:-1] - 1.0) * 100.0

    for i in np.where(move > MIN_MOVE_PCT)[0]:
        prev, cur = close[i], close[i + 1]
        if not (prev > 0 and cur > 0):
            continue
        factor = _match_factor(prev / cur)
        if factor is None:
            continue

        before = vol[max(0, i - WINDOW + 1):i + 1]
        after = vol[i + 1:i + 1 + WINDOW]
        if len(before) < 3 or len(after) < 3:
            continue
        med_before, med_after = float(np.median(before)), float(np.median(after))
        if med_before <= 0:
            continue

        vol_ratio = med_after / med_before
        if abs(vol_ratio - factor) / factor > VOLUME_TOL:
            continue
        # A crash or squeeze prints an extreme volume day; a split does not.
        if vol[i + 1] / max(med_before * factor, 1e-9) > EVENT_SPIKE_MAX:
            continue

        out.append(
            Adjustment(
                date=df.index[i + 1].strftime("%Y-%m-%d"),
                factor=factor,
                price_ratio=round(prev / cur, 4),
                volume_ratio=round(vol_ratio, 3),
            )
        )
    return out


def back_adjust(df: pd.DataFrame) -> tuple[pd.DataFrame, list[Adjustment]]:
    """Rescale history before each detected split so the series is continuous.

    Prices before a k-for-1 split are divided by k and volumes multiplied by k,
    which is what every charting package shows.
    """
    splits = detect_splits(df)
    if not splits:
        return df, []

    out = df.copy()
    for adj in splits:
        cut = pd.Timestamp(adj.date)
        before = out.index < cut
        if not before.any():
            continue
        for col in ("open", "high", "low", "close"):
            out.loc[before, col] = out.loc[before, col] / adj.factor
        out.loc[before, "volume"] = out.loc[before, "volume"] * adj.factor
    return out, splits
