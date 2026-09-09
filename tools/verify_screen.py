"""Check the screen's output against its own rules, for every stock.

Fixing logic against two charts proves nothing about the other 5,626. This
re-derives the rules independently from the price series and checks that every
detected base obeys them, rather than trusting the objects the detector built.

A violation here is a real bug: these are not thresholds or judgement calls,
they are properties the output must have if the rules were applied correctly.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import numpy as np

from screener import vcp
from screener.config import ROOT, load_config
from screener.fetch import PriceStore


def check(df, res, cfg) -> list[str]:
    """Return the list of rules this stock's result breaks."""
    bad: list[str] = []
    run = res.contractions
    if not run:
        return bad

    m = res.metrics
    v = cfg["vcp"]
    lows = df["low"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    n = len(df)

    # 1. Every consolidation low must never be undercut afterwards. This is the
    #    defining rule, re-derived straight from the price series.
    for c in run:
        if c.low_idx + 1 < n and float(np.min(lows[c.low_idx + 1:])) < c.low - 1e-6:
            bad.append(f"low of T{c.index} undercut later")

    # 2. Lows ascend. Depths are NOT required to contract - the run is the
    #    base's actual sequence, widening steps included - but every widening
    #    step must be marked, and no consolidation inside the base may be
    #    missing from it. That second rule is the one that matters: the old
    #    selection built a tidy shrinking subsequence and dropped the rest,
    #    which is how a base that went 9.2 -> 7.4 -> 4.7 -> 9.7 was published
    #    as a smooth 15.8 -> 15.1 -> 9.7.
    if [c.low for c in run] != sorted(c.low for c in run):
        bad.append("lows do not ascend")
    shrink = float(v.get("contraction_shrink_factor", 1.0))
    for i in range(1, len(run)):
        widens = run[i].depth_pct > run[i - 1].depth_pct * shrink
        if widens != run[i].widened:
            bad.append(f"T{i+1} widening flag does not match its depths")
    inside = [c for c in res.raw_cycles
              if run[0].high_idx <= c.high_idx <= run[-1].high_idx]
    if len(inside) != len(run):
        bad.append("a consolidation inside the base is missing from the run")

    # 3. Contractions are ordered and do not overlap.
    for i in range(len(run)):
        if run[i].high_idx >= run[i].low_idx:
            bad.append(f"T{run[i].index} high is not before its low")
        if i and run[i].high_idx <= run[i - 1].low_idx:
            bad.append(f"T{run[i].index} starts before the previous one ended")

    # 4. The trade levels come from the final consolidation.
    final = run[-1]
    if abs(m["pivot"] - final.high) > 1e-3:
        bad.append("pivot is not the final contraction's high")
    if abs(m["support"] - final.low) > 1e-3:
        bad.append("support is not the final contraction's low")

    # 5. The stop sits one buffer under the support line, and risk is measured
    #    from the stop rather than from the line.
    buf = float(v.get("stop_buffer_dollars", 0.10))
    if abs(m["stop"] - (final.low - buf)) > 1e-3:
        bad.append("stop is not one buffer under the support line")
    want_risk = (m["pivot"] - m["stop"]) / m["pivot"] * 100.0
    if abs(m["risk_pct"] - want_risk) > 0.02:
        bad.append("risk does not match pivot and stop")

    # 6. The entry ceiling is a fixed step above the pivot, so the buy range is
    #    never empty the way "stop x (1 + max risk)" could make it.
    ceiling = float(v.get("max_entry_above_pivot_pct", 5.0))
    if abs(m["max_entry"] - m["pivot"] * (1.0 + ceiling / 100.0)) > 1e-3:
        bad.append("max entry is not the configured step above the pivot")
    if m["max_entry"] < m["pivot"]:
        bad.append("buy range is empty - the ceiling is below the pivot")

    # 7. Profit levels and the break-even trigger are fixed steps off the pivot.
    r = cfg["risk"]
    for t, want in zip(m.get("profit_targets", []), r.get("profit_targets_pct", [])):
        if abs(t["price"] - m["pivot"] * (1.0 + float(want) / 100.0)) > 1e-3:
            bad.append(f"the +{want}% target is not that far above the pivot")
    be = float(r.get("breakeven_move_pct", 5.0))
    if abs(m["breakeven_trigger"] - m["pivot"] * (1.0 + be / 100.0)) > 1e-3:
        bad.append("break-even trigger is not the configured step above the pivot")

    # 6. Highs and lows must actually exist in the bars they point at.
    for c in run:
        if abs(highs[c.high_idx] - c.high) > 1e-6 or abs(lows[c.low_idx] - c.low) > 1e-6:
            bad.append(f"T{c.index} levels do not match the bars")

    # 7. Base length is measured from the first contraction's high.
    if m["base_length_bars"] != n - 1 - run[0].high_idx + 1:
        bad.append("base length does not match the first contraction")

    # 8. A pivot reported unbroken must really be unbroken.
    since = highs[final.low_idx:]
    if m["pivot_broken"] is False and since.size and float(np.max(since)) > m["pivot"] + 1e-6:
        bad.append("pivot reported unbroken but a high exceeded it")

    # 9. The reported pauses must be run members carrying the matching flag,
    #    and each flag must match the numbers behind it. A consolidation can
    #    be both deeper and heavier than the one before it, so the two lists
    #    may legitimately name the same one.
    by_date = {c.high_date: c for c in run}
    for w in m.get("widening_pauses", []):
        c = by_date.get(w["date"])
        if c is None or not c.widened:
            bad.append("a reported widening pause is not a marked run member")
        elif c.depth_pct <= run[c.index - 2].depth_pct:
            bad.append("a reported widening pause is not deeper than its predecessor")
    for w in m.get("volume_rose_pauses", []):
        c = by_date.get(w["date"])
        if c is None or not c.volume_rose:
            bad.append("a reported volume pause is not a marked run member")
        else:
            before = run[c.index - 2]
            if np.isfinite(c.avg_volume) and np.isfinite(before.avg_volume) \
               and c.avg_volume <= before.avg_volume:
                bad.append("a reported volume pause did not trade heavier")
    if m.get("perfect_vcp") != (not any(c.widened for c in run)):
        bad.append("perfect_vcp disagrees with the widening flags")


    return bad


def check_ready(res, cfg) -> list[str]:
    """A name in Ready must satisfy every hard rule, with no exceptions."""
    m, v, r = res.metrics, cfg["vcp"], cfg["risk"]
    bad = []
    if len(res.contractions) < int(v["min_contractions"]):
        bad.append("too few contractions")
    if m["risk_pct"] > float(r["max_risk_pct"]) + 1e-9:
        bad.append("risk over the ceiling")
    if m["reward_risk"] is None or m["reward_risk"] < float(r["min_reward_risk"]) - 1e-9:
        bad.append("reward:risk under the minimum")
    if m["pivot_broken"]:
        bad.append("pivot already broken")
    if m["base_length_bars"] < int(v["min_base_bars"]):
        bad.append("base too short")
    if not (float(v["final_depth_min_pct"]) <= m["final_depth_pct"] <= float(v["final_depth_max_pct"])):
        bad.append("final contraction outside the range")
    if m["volume_dryup_ratio"] is not None and m["volume_dryup_ratio"] > float(v["dryup_ratio"]):
        bad.append("no volume dry-up")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(ROOT / "cache" / "prices.sqlite"))
    ap.add_argument("--results", default=str(ROOT / "docs" / "data" / "screen.json"))
    args = ap.parse_args()

    cfg = load_config()
    store = PriceStore(args.cache)
    hist = int(cfg["data"]["history_days"])
    symbols = list(store.last_dates())

    checked = with_base = 0
    violations: Counter[str] = Counter()
    examples: dict[str, str] = {}

    for sym in symbols:
        df = store.load(sym, hist)
        if len(df) < int(cfg["universe"]["min_history_days"]):
            continue
        checked += 1
        try:
            res = vcp.detect(df, cfg)
        except Exception as exc:  # noqa: BLE001
            violations[f"detect raised {type(exc).__name__}"] += 1
            examples.setdefault(f"detect raised {type(exc).__name__}", sym)
            continue
        if not res.contractions:
            continue
        with_base += 1
        for problem in check(df, res, cfg):
            violations[problem] += 1
            examples.setdefault(problem, sym)

    print(f"symbols checked      : {checked}")
    print(f"with a detected base : {with_base}")
    print(f"\nrule violations      : {sum(violations.values())}")
    if violations:
        for problem, count in violations.most_common():
            print(f"   {count:5d}  {problem}   (e.g. {examples[problem]})")
    else:
        print("   none - every detected base obeys the rules")

    # Cross-check the published Ready list against the hard rules.
    results = Path(args.results)
    if results.exists():
        data = json.loads(results.read_text())
        print(f"\nready names re-checked from source data: {len(data['ready'])}")
        for row in data["ready"]:
            df = store.load(row["symbol"], hist)
            res = vcp.detect(df, cfg)
            problems = check(df, res, cfg) + check_ready(res, cfg)
            depths = [round(c.depth_pct, 2) for c in res.contractions]
            flag = "OK " if not problems else "BAD"
            print(f"   {flag} {row['symbol']:6s} depths={depths} "
                  f"risk={res.metrics['risk_pct']}% rr={res.metrics['reward_risk']} "
                  f"perfect={res.metrics['perfect_vcp']}")
            for p in problems:
                print(f"        !! {p}")

    store.close()
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
