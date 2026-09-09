"""Volatility Contraction Pattern detection.

The brief defines a consolidation by when it *ends*: "price drops to A, bounces,
breaks below A -> A was only a midpoint; price drops to B and never goes below B
again -> B is the low of that consolidation." That rule is what this module
implements literally, via nested extremes:

    H1 = highest high in the base window
    L1 = lowest low after H1        -> by construction nothing after L1 is lower
    H2 = highest high after L1      -> by construction H2 <= H1
    L2 = lowest low after H2        -> by construction L2 >= L1
    ...

Each (Hn, Ln) pair is one complete T cycle: a drop to a relative low that is
never taken out, followed by a recovery. The construction guarantees the
descending-highs / ascending-lows wedge of a VCP, so the screen's real job is
checking that the *depths* and *volumes* shrink from one T to the next.

The final T supplies the trade: its high is the pivot (resistance to break) and
its low is the support (stop-loss reference).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any

import numpy as np
import pandas as pd

# Anything shallower than this is noise, not a consolidation.
MIN_MEANINGFUL_DEPTH_PCT = 1.0
# Hard cap on how many raw cycles we walk before selecting the trailing run.
MAX_RAW_CONTRACTIONS = 12


@dataclass
class Contraction:
    index: int                 # 1-based T number within the selected run
    high_idx: int
    low_idx: int
    high_date: str
    low_date: str
    high: float
    low: float
    depth_pct: float
    bars: int
    avg_volume: float

    def as_dict(self) -> dict:
        d = asdict(self)
        d["high"] = round(self.high, 4)
        d["low"] = round(self.low, 4)
        d["depth_pct"] = round(self.depth_pct, 2)
        d["avg_volume"] = round(self.avg_volume, 0)
        return d


@dataclass
class VCPResult:
    passed: bool
    status: str = "none"          # breakout | actionable | forming | extended | none
    reason: str = ""
    contractions: list[Contraction] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "status": self.status,
            "reason": self.reason,
            "contractions": [c.as_dict() for c in self.contractions],
            "metrics": self.metrics,
        }


def _walk_cycles(
    highs: np.ndarray,
    lows: np.ndarray,
    vols: np.ndarray,
    dates: list[str],
    start: int,
    end: int,
    min_swing_bars: int,
    min_bars_since_low: int,
) -> list[Contraction]:
    """Walk the nested-extreme staircase from `start` to `end` (inclusive)."""
    out: list[Contraction] = []
    if end - start < min_swing_bars * 2:
        return out

    # A high can only start a consolidation if there is room after it for a
    # decline *and* for the resulting low to prove it holds. Without this
    # bound a stock breaking out to a new high would make today's bar the
    # running maximum, and the walk would stop before seeing the base that
    # produced the breakout.
    search_end = end - (min_swing_bars + min_bars_since_low)
    if search_end <= start:
        return out

    high_idx = int(np.argmax(highs[start : search_end + 1])) + start

    while len(out) < MAX_RAW_CONTRACTIONS:
        seg_start = high_idx + 1
        # Need room for a decline and for the low to then prove it holds.
        if seg_start + min_swing_bars > end:
            break

        low_idx = int(np.argmin(lows[seg_start : end + 1])) + seg_start
        high_p = float(highs[high_idx])
        low_p = float(lows[low_idx])
        if high_p <= 0:
            break
        depth = (high_p - low_p) / high_p * 100.0

        # A consolidation only counts once its low has held for a few bars -
        # otherwise we would be calling today's dip a finished T cycle.
        held_bars = end - low_idx
        if depth < MIN_MEANINGFUL_DEPTH_PCT or held_bars < min_bars_since_low:
            break

        seg_vol = vols[high_idx : low_idx + 1]
        out.append(
            Contraction(
                index=len(out) + 1,
                high_idx=high_idx,
                low_idx=low_idx,
                high_date=dates[high_idx],
                low_date=dates[low_idx],
                high=high_p,
                low=low_p,
                depth_pct=depth,
                bars=low_idx - high_idx + 1,
                avg_volume=float(np.mean(seg_vol)) if seg_vol.size else float("nan"),
            )
        )

        nxt = low_idx + 1
        if nxt > search_end:
            break
        next_high_idx = int(np.argmax(highs[nxt : search_end + 1])) + nxt
        if next_high_idx <= high_idx:
            break
        high_idx = next_high_idx

    return out


def _select_run(cycles: list[Contraction], cfg: dict) -> list[Contraction]:
    """Pick the longest trailing run whose depth (and volume) keeps shrinking."""
    if not cycles:
        return []
    shrink = float(cfg.get("contraction_shrink_factor", 0.85))
    vol_shrink = float(cfg.get("volume_shrink_factor", 1.0))
    check_vol = bool(cfg.get("require_volume_contraction", True))
    higher_lows = bool(cfg.get("require_higher_lows", True))
    tol = float(cfg.get("higher_low_tolerance_pct", 0.5)) / 100.0
    max_n = int(cfg.get("max_contractions", 6))

    run = [cycles[-1]]
    for prev in reversed(cycles[:-1]):
        nxt = run[0]
        if nxt.depth_pct > prev.depth_pct * shrink:
            break
        if higher_lows and nxt.low < prev.low * (1.0 - tol):
            break
        if check_vol and np.isfinite(prev.avg_volume) and np.isfinite(nxt.avg_volume):
            if nxt.avg_volume > prev.avg_volume * vol_shrink:
                break
        run.insert(0, prev)
        if len(run) >= max_n:
            break

    for i, c in enumerate(run, start=1):
        c.index = i
    return run


def detect(df: pd.DataFrame, cfg: dict) -> VCPResult:
    """Find the current VCP base, if any, in a symbol's daily bars."""
    v = cfg.get("vcp", {})
    risk_cfg = cfg.get("risk", {})

    n = len(df)
    lookback = int(v.get("base_lookback_days", 130))
    if n < max(lookback // 2, 40):
        return VCPResult(False, "none", "not enough history")

    start = max(0, n - lookback)
    end = n - 1
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    vols = df["volume"].to_numpy(dtype=float)
    dates = [d.strftime("%Y-%m-%d") for d in df.index]

    cycles = _walk_cycles(
        highs,
        lows,
        vols,
        dates,
        start,
        end,
        int(v.get("min_swing_bars", 3)),
        int(v.get("min_bars_since_final_low", 3)),
    )
    if not cycles:
        return VCPResult(False, "none", "no completed consolidation in the base window")

    run = _select_run(cycles, v)
    first, final = run[0], run[-1]

    pivot = final.high
    support = final.low
    price = float(closes[-1])
    risk_pct = (pivot - support) / pivot * 100.0 if pivot > 0 else float("nan")

    base_high = float(np.max(highs[first.high_idx : end + 1]))
    base_low = float(np.min(lows[first.high_idx : end + 1]))
    target = pivot + (base_high - base_low)
    reward = target - pivot
    risk_abs = pivot - support
    rr = reward / risk_abs if risk_abs > 0 else float("nan")

    dry_days = int(v.get("dryup_days", 5))
    avg_50 = float(np.mean(vols[-50:])) if n >= 50 else float("nan")

    # Volume dry-up is a property of the final consolidation, not of today.
    # Measuring it over the trailing few sessions breaks the moment the stock
    # breaks out: the breakout's own volume surge lands inside the window and
    # the pattern is rejected for "no dry-up" exactly when it triggers. So
    # compare the final contraction's own volume against the 50-day average as
    # it stood at that contraction's low.
    base_end = final.low_idx + 1
    base_start = max(0, base_end - 50)
    avg_50_at_low = float(np.mean(vols[base_start:base_end])) if base_end > base_start else float("nan")
    dryup = (
        final.avg_volume / avg_50_at_low
        if np.isfinite(avg_50_at_low) and avg_50_at_low > 0 and np.isfinite(final.avg_volume)
        else float("nan")
    )

    # Kept for display: how quiet the last few sessions have been. Informative,
    # but deliberately not part of the pass/fail decision.
    avg_recent = float(np.mean(vols[-dry_days:])) if n >= dry_days else float("nan")
    recent_vs_50 = (
        avg_recent / avg_50 if np.isfinite(avg_50) and avg_50 > 0 else float("nan")
    )

    dist_pct = (price - pivot) / pivot * 100.0 if pivot > 0 else float("nan")
    last_vol = float(vols[-1])
    vol_multiple = last_vol / avg_50 if np.isfinite(avg_50) and avg_50 > 0 else float("nan")

    metrics = {
        "contraction_count": len(run),
        "pivot": round(pivot, 4),
        "support": round(support, 4),
        "pivot_date": final.high_date,
        "support_date": final.low_date,
        "risk_pct": round(risk_pct, 2),
        "target": round(target, 4),
        "reward_risk": round(rr, 2) if np.isfinite(rr) else None,
        "base_high": round(base_high, 4),
        "base_low": round(base_low, 4),
        "base_depth_pct": round((base_high - base_low) / base_high * 100.0, 2)
        if base_high > 0
        else None,
        "base_start_date": first.high_date,
        "base_length_bars": end - first.high_idx + 1,
        "first_depth_pct": round(first.depth_pct, 2),
        "final_depth_pct": round(final.depth_pct, 2),
        "distance_to_pivot_pct": round(dist_pct, 2),
        "volume_dryup_ratio": round(dryup, 3) if np.isfinite(dryup) else None,
        "recent_volume_vs_50d": round(recent_vs_50, 3) if np.isfinite(recent_vs_50) else None,
        "volume_vs_50d": round(vol_multiple, 2) if np.isfinite(vol_multiple) else None,
        "bars_since_support": end - final.low_idx,
    }

    # --- validation -------------------------------------------------------
    fails: list[str] = []
    if len(run) < int(v.get("min_contractions", 2)):
        fails.append(
            f"only {len(run)} contraction(s), need {int(v.get('min_contractions', 2))}"
        )
    if first.depth_pct > float(v.get("max_first_depth_pct", 35.0)):
        fails.append(f"T1 too deep ({first.depth_pct:.1f}%)")
    lo_f = float(v.get("final_depth_min_pct", 2.0))
    hi_f = float(v.get("final_depth_max_pct", 12.0))
    if not (lo_f <= final.depth_pct <= hi_f):
        fails.append(f"final T {final.depth_pct:.1f}% outside {lo_f:g}-{hi_f:g}%")
    if np.isfinite(dryup) and dryup > float(v.get("dryup_ratio", 0.85)):
        fails.append(f"final T volume {dryup:.2f}x its 50d average, no dry-up")
    if price <= support:
        fails.append("price has broken the support line")

    max_risk = float(risk_cfg.get("max_risk_pct", 5.0))
    if not np.isfinite(risk_pct) or risk_pct > max_risk:
        fails.append(f"risk {risk_pct:.1f}% above {max_risk:g}% ceiling")
    min_rr = float(risk_cfg.get("min_reward_risk", 3.0))
    if not np.isfinite(rr) or rr < min_rr:
        fails.append(f"reward:risk {rr:.1f} below {min_rr:g}")

    # --- where in the pattern are we? ------------------------------------
    #
    # The entry is a break *above* the pivot. Once any session has traded
    # through it the entry has already happened, and a nightly screen only sees
    # that after the fact - so a stock above its pivot is a missed trade, not a
    # candidate. Both the last close and every high since the support low are
    # checked: a bar that pierced the pivot intraday took the entry with it,
    # even if it closed back below.
    max_below = float(v.get("max_distance_below_pivot_pct", 15.0))
    max_above = float(v.get("max_extended_above_pivot_pct", 5.0))
    tol = float(v.get("pivot_break_tolerance_pct", 0.0)) / 100.0

    high_since_support = float(np.max(highs[final.low_idx : end + 1]))
    pivot_broken = pivot > 0 and high_since_support > pivot * (1.0 + tol)

    if dist_pct > max_above:
        status = "extended"
    elif pivot_broken or dist_pct > 0:
        status = "broke_out"
    elif dist_pct >= -max_below:
        status = "actionable"
    else:
        status = "forming"

    metrics["high_since_support"] = round(high_since_support, 4)
    metrics["pivot_broken"] = bool(pivot_broken)

    if status == "broke_out":
        fails.append(
            f"pivot already broken (high {high_since_support:.2f} vs pivot {pivot:.2f})"
        )
    elif status in ("extended", "forming"):
        fails.append(f"price {dist_pct:+.1f}% from pivot ({status})")

    return VCPResult(
        passed=not fails,
        status=status,
        reason="; ".join(fails),
        contractions=run,
        metrics=metrics,
    )
