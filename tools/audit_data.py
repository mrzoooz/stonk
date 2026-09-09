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

from screener import adjust
from screener.config import ROOT, load_config
from screener.fetch import PriceStore

# Ratios an actual split produces. Deliberately excludes 1.25, 1.33, 1.5 and
# 2.5: those sit inside the range of ordinary single-day moves in volatile
# small caps, so including them flags hundreds of real price moves as splits.
SPLIT_RATIOS = [2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 50]


def near_split(ratio: float, tol: float = 0.015) -> float | None:
    """A price ratio close enough to a whole-number split to be worth checking."""
    for r in SPLIT_RATIOS:
        for cand in (r, 1 / r):
            if abs(ratio - cand) / cand <= tol:
                return cand
    return None


def volume_corroborates(vols, i: int, price_ratio: float, window: int = 10) -> tuple[bool, float]:
    """Does share volume step the way a split would?

    Nasdaq serves unadjusted volume too, so a k-for-1 forward split should
    roughly multiply share volume by k while dividing the price by k. A real
    price move has no such relationship, which is what separates the two.
    """
    before = vols[max(0, i - window + 1):i + 1]
    after = vols[i + 1:i + 1 + window]
    if len(before) < 3 or len(after) < 3:
        return False, float("nan")
    med_before, med_after = float(np.median(before)), float(np.median(after))
    if med_before <= 0:
        return False, float("nan")
    observed = med_after / med_before
    expected = price_ratio                      # price/k  <->  volume*k
    if expected <= 0:
        return False, observed
    return abs(observed - expected) / expected <= 0.45, observed


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
    big_moves = 0
    ratio_shaped: dict[str, list] = {}
    suspects: dict[str, list] = {}
    hist = int(cfg.get("data", {}).get("history_days", 800))
    for sym in last_dates:
        df = store.load(sym, hist)
        if len(df) < 30:
            continue
        c = df["close"].to_numpy(dtype=float)
        v = df["volume"].to_numpy(dtype=float)
        prev, cur = c[:-1], c[1:]
        with np.errstate(divide="ignore", invalid="ignore"):
            change = np.abs(cur / prev - 1.0) * 100.0
        for i in np.where(change > args.jump_pct)[0]:
            if prev[i] <= 0 or cur[i] <= 0:
                continue
            big_moves += 1
            ratio = prev[i] / cur[i]
            split = near_split(ratio)
            if not split:
                continue
            ratio_shaped.setdefault(sym, []).append(i)
            ok, observed = volume_corroborates(v, i, ratio)
            if ok:
                # How far the event day's own volume sits above what the split
                # alone would explain - the test that separates a split from a
                # crash, and the one the scan applies.
                med_before = float(np.median(v[max(0, i - 10 + 1):i + 1]))
                spike = v[i + 1] / max(med_before * split, 1e-9)
                suspects.setdefault(sym, []).append(
                    (df.index[i + 1].strftime("%Y-%m-%d"), round(prev[i], 2),
                     round(cur[i], 2), round(ratio, 3), round(observed, 2), round(spike, 2))
                )
    print(f"    single-day moves over {args.jump_pct:.0f}%          : {big_moves}")
    print(f"    of those, near a whole split ratio : {sum(len(x) for x in ratio_shaped.values())}"
          f" across {len(ratio_shaped)} symbols")
    print(f"    AND corroborated by a volume step  : {len(suspects)} symbols")

    # Reconcile against what the scan will actually rescale. A candidate the
    # scan declines is either a crash caught by the event-day volume test, or a
    # sign the threshold is too tight - so print the number either way.
    adjusted, declined = [], []
    for sym in sorted(suspects):
        df = store.load(sym, hist)
        found = adjust.detect_splits(df)
        (adjusted if found else declined).append(sym)

    print(f"\n    the scan back-adjusts              : {len(adjusted)} symbols")
    print(f"    candidates the scan declines       : {len(declined)} symbols")
    print("      (spike = event-day volume over what the split alone explains;"
          f" scan rejects above {adjust.EVENT_SPIKE_MAX})")
    for sym in declined[:20]:
        d, a, b, r, vr, spike = suspects[sym][0]
        print(f"      {sym:8s} {d}  {a} -> {b}  ratio {r}, vol {vr}, spike {spike}")
    print("    back-adjusted:")
    for sym in adjusted[:20]:
        d, a, b, r, vr, spike = suspects[sym][0]
        print(f"      {sym:8s} {d}  {a} -> {b}  ratio {r}, vol {vr}, spike {spike}")

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
