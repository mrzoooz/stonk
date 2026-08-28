"""Ranking candidates.

Every stock that reaches this point already satisfies the hard rules, so the
score exists only to order the list: tighter final contraction, better
reward:risk, drier volume and stronger relative strength rank higher.
"""
from __future__ import annotations

import numpy as np


def _clamp01(x: float) -> float:
    if not np.isfinite(x):
        return 0.0
    return float(min(1.0, max(0.0, x)))


def _scale(value: float, lo: float, hi: float, invert: bool = False) -> float:
    """Map `value` from [lo, hi] onto [0, 1], optionally reversed."""
    if not np.isfinite(value) or hi == lo:
        return 0.0
    t = _clamp01((value - lo) / (hi - lo))
    return 1.0 - t if invert else t


COMPONENTS = (
    # (key, weight, lo, hi, invert)
    ("reward_risk", 0.26, 2.0, 10.0, False),
    ("risk_pct", 0.18, 1.0, 5.0, True),
    ("final_depth_pct", 0.14, 2.0, 12.0, True),
    ("volume_dryup_ratio", 0.14, 0.30, 0.90, True),
    ("contraction_count", 0.10, 2.0, 4.0, False),
    ("rs_126d", 0.12, 0.0, 40.0, False),
    ("distance_to_pivot_pct", 0.06, -12.0, 0.0, False),
)


def score(vcp_metrics: dict, stage2_metrics: dict) -> float:
    """Composite 0-100 score. Higher is a tighter, better-shaped setup."""
    merged = {**stage2_metrics, **vcp_metrics}
    total = 0.0
    for key, weight, lo, hi, invert in COMPONENTS:
        raw = merged.get(key)
        total += weight * _scale(float(raw) if raw is not None else float("nan"), lo, hi, invert)
    return round(total * 100.0, 1)
