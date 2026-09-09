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

    # 2. Lows ascend, and the sequence contracts.
    if [c.low for c in run] != sorted(c.low for c in run):
        bad.append("lows do not ascend")
    depths = [c.depth_pct for c in run]
    shrink = float(v.get("contraction_shrink_factor", 1.0))
    for i in range(1, len(depths)):
        if depths[i] > depths[i - 1] * shrink + 1e-9:
            bad.append(f"T{i+1} wider than T{i}")

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

    # 5. Risk is definitionally the final contraction's depth.
    if abs(m["risk_pct"] - final.depth_pct) > 0.02:
        bad.append("risk does not equal the final contraction depth")

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

    # 9. Recorded widening pauses must actually be narrower than what follows -
    #    that shallower-then-deeper step is the whole reason they are recorded.
    for w in m.get("widening_pauses", []):
        after = [c for c in run if c.high_date > w["date"]]
        if after and w["depth_pct"] >= after[0].depth_pct:
            bad.append("a recorded widening pause is not narrower than its successor")

    # 10. A pause recorded as trading heavier must really precede a heavier one,
    #     and must not be double-reported as a widening pause.
    widened = {w["date"] for w in m.get("widening_pauses", [])}
    for w in m.get("volume_rose_pauses", []):
        if w["date"] in widened:
            bad.append("a pause is reported as both widening and heavier-volume")
        after = [c for c in run if c.high_date > w["date"]]
        if after and np.isfinite(after[0].avg_volume):
            prior = [c for c in res.raw_cycles if c.high_date == w["date"]]
            if prior and np.isfinite(prior[0].avg_volume):
                if after[0].avg_volume <= prior[0].avg_volume:
                    bad.append("a pause reported as heavier-volume is not")

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
