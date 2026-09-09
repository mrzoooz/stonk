"""Audit the cached price data for problems the screen cannot see by itself.

The screening engines are tested against synthetic patterns, which cannot catch
data-integrity faults. This inspects the real cache and reports:

  1. Stale symbols - a delisted or halted stock keeps its history and would be
     screened as if its last close were today's.
  2. Split-like discontinuities - Nasdaq's endpoint serves unadjusted closes, so
     a split puts a step change in the series that corrupts every moving
     average and can read as a huge fake contraction.
  3. Whether any of those symbols actually reached the published results.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import numpy as np

from screener.config import ROOT, load_config
from screener.fetch import PriceStore

# Ratios a split produces. Anything landing near one of these is very likely a
# corporate action rather than a real price move.
SPLIT_RATIOS = [2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 1.5, 2.5, 3 / 2, 4 / 3, 5 / 4, 5 / 3]


def near_split(ratio: float, tol: float = 0.04) -> float | None:
    for r in SPLIT_RATIOS:
        for cand in (r, 1 / r):
            if abs(ratio - cand) / cand <= tol:
                return cand
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(ROOT / "cache" / "prices.sqlite"))
    ap.add_argument("--results", default=str(ROOT / "docs" / "data" / "screen.json"))
    ap.add_argument("--stale-days", type=int, default=7)
    ap.add_argument("--jump-pct", type=float, default=35.0)
    args = ap.parse_args()

    cfg = load_config()
    store = PriceStore(args.cache)
    last_dates = store.last_dates()
    if not last_dates:
        print("cache is empty - nothing to audit")
        return 0

    market_date = max(last_dates.values())
    print(f"symbols in cache : {len(last_dates)}")
    print(f"latest session   : {market_date}")

    # --- 1. staleness ----------------------------------------------------
    md = datetime.strptime(market_date, "%Y-%m-%d").date()
    stale = {}
    for sym, last in last_dates.items():
        try:
            behind = (md - datetime.strptime(last, "%Y-%m-%d").date()).days
        except ValueError:
            continue
        if behind > args.stale_days:
            stale[sym] = behind
    print(f"\n[1] stale symbols (> {args.stale_days}d behind): {len(stale)}")
    for sym, behind in sorted(stale.items(), key=lambda kv: -kv[1])[:15]:
        print(f"      {sym:8s} {behind:5d}d behind (last {last_dates[sym]})")

    # --- 2. split-like jumps --------------------------------------------
    print(f"\n[2] scanning for close-to-close jumps over {args.jump_pct:.0f}% ...")
    suspects: dict[str, list] = {}
    hist = int(cfg.get("data", {}).get("history_days", 800))
    for sym in last_dates:
        df = store.load(sym, hist)
        if len(df) < 30:
            continue
        c = df["close"].to_numpy(dtype=float)
        prev, cur = c[:-1], c[1:]
        with np.errstate(divide="ignore", invalid="ignore"):
            change = np.abs(cur / prev - 1.0) * 100.0
        hits = np.where(change > args.jump_pct)[0]
        for i in hits:
            if prev[i] <= 0:
                continue
            ratio = prev[i] / cur[i]
            split = near_split(ratio)
            if split:
                suspects.setdefault(sym, []).append(
                    (df.index[i + 1].strftime("%Y-%m-%d"), round(prev[i], 2),
                     round(cur[i], 2), round(ratio, 3))
                )
    print(f"    symbols with split-shaped steps: {len(suspects)}")
    for sym, hits in list(sorted(suspects.items()))[:15]:
        d, a, b, r = hits[0]
        print(f"      {sym:8s} {d}  {a} -> {b}  (ratio {r})")

    # --- 3. did any of this reach the published results? -----------------
    results = Path(args.results)
    if results.exists():
        data = json.loads(results.read_text())
        reported = {}
        for bucket in ("ready", "watch", "stage2"):
            for row in data.get(bucket, []):
                reported[row["symbol"]] = bucket
        bad_stale = {s: b for s, b in reported.items() if s in stale}
        bad_split = {s: b for s, b in reported.items() if s in suspects}
        print(f"\n[3] published rows: {len(reported)}")
        print(f"    reported but stale : {bad_stale or 'none'}")
        print(f"    reported but split : {bad_split or 'none'}")
        buckets = Counter(reported.values())
        print(f"    buckets            : {dict(buckets)}")
    else:
        print("\n[3] no results file to cross-check")

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
