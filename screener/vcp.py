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
# Safety bound only. The walk terminates on its own because each cycle consumes
# bars and the high index strictly increases, so this exists purely to stop a
# pathological input looping - it must stay well above any real base's cycle
# count, because halting the walk early would leave the most recent cycles
# unseen and hand back a stale pivot from the middle of the base.
MAX_RAW_CONTRACTIONS = 200


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
    # How this consolidation compares with the one before it in the base.
    widened: bool = False
    volume_rose: bool = False

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
    # Every consolidation the walk found, selected or not. Not published - it
    # exists so the verifier can re-check what the selection stepped over.
    raw_cycles: list[Contraction] = field(default_factory=list)

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
    """Find each completed consolidation between `start` and `end`.

    The defining rule is the brief's own: a consolidation is finished when price
    reaches a low that is never taken out again. So the consolidation lows are
    exactly the bars whose low is the minimum of everything from that bar
    onward - which makes the sequence of lows ascending by construction.

    Each low is then paired with the peak that preceded it, taken from after
    the previous consolidation ended.

    Note what is deliberately *not* required: that the highs descend. A VCP can
    step upward - each consolidation forming above the last, at a higher price,
    after breaking out of it - while still contracting, because contraction is
    about the depth of each pause, not about a fixed ceiling. An earlier version
    anchored on the highest high in the window and walked forward from there,
    which silently imposed descending highs and made every stepped base look
    like a single consolidation.
    """
    out: list[Contraction] = []
    if end - start < min_swing_bars * 2:
        return out

    # suffix_min[i] is the lowest low from bar i to the end of the window, so
    # lows[i] == suffix_min[i] marks a low that nothing later undercuts.
    window = lows[start : end + 1]
    suffix_min = np.minimum.accumulate(window[::-1])[::-1]
    holds = np.flatnonzero(window <= suffix_min + 1e-12) + start

    seg_start = start
    for raw_low_idx in holds:
        low_idx = int(raw_low_idx)
        # The low has to sit far enough after the peak to be a decline, and far
        # enough before the end to have proved it holds.
        if low_idx <= seg_start or end - low_idx < min_bars_since_low:
            continue
        high_idx = int(np.argmax(highs[seg_start:low_idx])) + seg_start
        if low_idx - high_idx < min_swing_bars:
            continue

        high_p, low_p = float(highs[high_idx]), float(lows[low_idx])
        if high_p <= 0:
            continue
        depth = (high_p - low_p) / high_p * 100.0
        if depth < MIN_MEANINGFUL_DEPTH_PCT:
            continue

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
        seg_start = low_idx + 1
        if len(out) >= MAX_RAW_CONTRACTIONS:
            break

    return out


def _select_run(cycles: list[Contraction], cfg: dict) -> list[Contraction]:
    """The base's consolidations, in order, with nothing left out.

    Walks back from the newest consolidation taking every one it meets, and
    stops where the structure says the base began. An earlier version kept
    only the subset that formed a tidy shrinking sequence and silently dropped
    the rest, which reported a cleaner pattern than the chart showed - a base
    that went 9.2% -> 7.4% -> 4.7% -> 9.7% was published as a smooth
    15.8% -> 15.1% -> 9.7%. A step that widens now stays in the run and is
    marked, so the sequence on screen is the sequence in the price.

    The walk back ends at the first of:
      * a low that undercuts the one after it - the base's rising floor is
        broken, so anything earlier belongs to a different structure;
      * a consolidation whose high sits below the base's floor - price has
        advanced away from that region, so it is part of the run-up rather
        than of this base;
      * the safety cap on how many consolidations one base may hold.
    """
    if not cycles:
        return []
    shrink = float(cfg.get("contraction_shrink_factor", 1.0))
    vol_shrink = float(cfg.get("volume_shrink_factor", 1.0))
    check_vol = bool(cfg.get("require_volume_contraction", True))
    higher_lows = bool(cfg.get("require_higher_lows", True))
    tol = float(cfg.get("higher_low_tolerance_pct", 0.5)) / 100.0
    max_n = int(cfg.get("max_contractions", 15))

    # A base is a region price ranges in, not a whole advance. Without this
    # bound the walk runs back down months of uptrend collecting pullbacks
    # that were never part of the same base.
    height = float(cfg.get("max_base_height_pct", 25.0)) / 100.0
    floor = cycles[-1].high * (1.0 - height)

    run = [cycles[-1]]
    for prev in reversed(cycles[:-1]):
        nxt = run[0]
        if higher_lows and nxt.low < prev.low * (1.0 - tol):
            break
        if prev.high < floor:
            break
        if len(run) >= max_n:
            break
        run.insert(0, prev)

    for i, c in enumerate(run, start=1):
        c.index = i
        before = run[i - 2] if i > 1 else None
        c.widened = bool(before and c.depth_pct > before.depth_pct * shrink)
        c.volume_rose = bool(
            before
            and check_vol
            and np.isfinite(c.avg_volume)
            and np.isfinite(before.avg_volume)
            and c.avg_volume > before.avg_volume * vol_shrink
        )
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
    # The stop sits a little under the support line, not on it: a stop exactly
    # on the low is taken out by any wick that matches the prior low.
    stop = support - float(v.get("stop_buffer_dollars", 0.10))
    price = float(closes[-1])
    risk_pct = (pivot - stop) / pivot * 100.0 if pivot > 0 else float("nan")

    base_high = float(np.max(highs[first.high_idx : end + 1]))
    base_low = float(np.min(lows[first.high_idx : end + 1]))
    target = pivot + (base_high - base_low)
    reward = target - pivot
    risk_abs = pivot - stop
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
        "stop": round(stop, 4),
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
    if first.depth_pct > float(v.get("max_first_depth_pct", 50.0)):
        fails.append(
            f"T1 too deep ({first.depth_pct:.1f}%) - a fall past "
            f"{float(v.get('max_first_depth_pct', 50.0)):.0f}% means stage 4 has not finished"
        )

    # A base that has barely formed carries no information, and its measured
    # move is degenerate because the base has almost no depth to project.
    min_bars = int(v.get("min_base_bars", 20))
    if metrics["base_length_bars"] < min_bars:
        fails.append(
            f"base only {metrics['base_length_bars']} sessions, need {min_bars}"
        )
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

    # Chasing a breakout gives up the move you were positioning for, so there
    # is a highest price still worth paying: buy between the pivot and this,
    # never above it.
    metrics["max_entry"] = round(
        pivot * (1.0 + float(v.get("max_entry_above_pivot_pct", 5.0)) / 100.0), 4
    )

    # Which of the preferred levels this base meets, for the app to colour.
    base_bars = metrics["base_length_bars"]
    shrink_ratios = [
        run[i].depth_pct / run[i - 1].depth_pct
        for i in range(1, len(run))
        if run[i - 1].depth_pct > 0
    ]
    metrics["preferred"] = {
        "contractions": len(run) >= int(v.get("preferred_contractions", 3)),
        "base_length": base_bars >= int(v.get("preferred_base_bars", 63)),
        "first_depth": first.depth_pct <= float(v.get("preferred_first_depth_pct", 30.0)),
        "final_depth": final.depth_pct <= float(v.get("preferred_final_depth_pct", 5.0)),
        "shrink": bool(shrink_ratios) and max(shrink_ratios)
                  <= float(v.get("preferred_shrink_factor", 0.5)),
    }
    # >1 means the base widened at its worst step; the run is no longer
    # guaranteed to shrink monotonically now that nothing is dropped from it.
    metrics["worst_shrink_ratio"] = round(max(shrink_ratios), 3) if shrink_ratios else None

    # Every widening step in the base, reported from the run itself rather
    # than from a list of things the walk threw away. "If the second
    # consolidation has more volatility than the previous one, it's not a
    # perfect VCP" - recorded, not disqualifying.
    widened = [c for c in run if c.widened]
    metrics["widening_pauses"] = [
        {"date": c.high_date, "depth_pct": round(c.depth_pct, 2), "index": c.index}
        for c in widened
    ]
    metrics["perfect_vcp"] = not widened

    heavier = [c for c in run if c.volume_rose]
    metrics["volume_rose_pauses"] = [
        {"date": c.high_date, "depth_pct": round(c.depth_pct, 2), "index": c.index}
        for c in heavier
    ]

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
        raw_cycles=cycles,
    )


def explain(df: pd.DataFrame, cfg: dict) -> dict:
    """Show the walk for one symbol: what it saw, and what it ignored.

    The walk anchors on the highest high in the base window and only moves
    forward from there, so any consolidation that formed *before* that high is
    outside the base by construction. That is deliberate - a rally to a new
    high means the earlier base resolved, and a VCP's contractions have
    descending highs - but it is invisible in the output, so this makes it
    explicit.
    """
    v = cfg.get("vcp", {})
    n = len(df)
    lookback = int(v.get("base_lookback_days", 130))
    start, end = max(0, n - lookback), n - 1

    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    vols = df["volume"].to_numpy(dtype=float)
    dates = [d.strftime("%Y-%m-%d") for d in df.index]

    min_swing = int(v.get("min_swing_bars", 3))
    min_hold = int(v.get("min_bars_since_final_low", 3))
    search_end = end - (min_swing + min_hold)
    anchor = int(np.argmax(highs[start : search_end + 1])) + start if search_end > start else start

    raw = _walk_cycles(highs, lows, vols, dates, start, end, min_swing, min_hold)
    run = _select_run(raw, v) if raw else []
    result = detect(df, cfg)

    return {
        "window": {
            "from": dates[start], "to": dates[end], "bars": end - start + 1,
            "high": round(float(np.max(highs[start:end + 1])), 2),
            "low": round(float(np.min(lows[start:end + 1])), 2),
        },
        "anchor": {
            "date": dates[anchor], "high": round(float(highs[anchor]), 2),
            "bars_before_anchor_ignored": anchor - start,
            "range_before_anchor": {
                "high": round(float(np.max(highs[start:anchor + 1])), 2),
                "low": round(float(np.min(lows[start:anchor + 1])), 2),
            } if anchor > start else None,
        },
        "raw_cycles": [
            {"from": c.high_date, "to": c.low_date, "high": round(c.high, 2),
             "low": round(c.low, 2), "depth_pct": round(c.depth_pct, 2)}
            for c in raw
        ],
        "selected": [
            {"T": c.index, "from": c.high_date, "to": c.low_date,
             "high": round(c.high, 2), "low": round(c.low, 2),
             "depth_pct": round(c.depth_pct, 2)}
            for c in run
        ],
        "status": result.status,
        "passed": result.passed,
        "reason": result.reason,
    }
