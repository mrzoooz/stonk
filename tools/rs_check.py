"""Show how the RS Rating for a symbol was arrived at.

The rating is computed here, not sourced from IBD or TradingView, so when it
disagrees with an outside number the only way to settle it is to look at the
inputs: the raw price legs, the score they produce, and where that score sits
in the field it was ranked against.
"""
from __future__ import annotations

import argparse

import numpy as np

from screener import indicators as ind
from screener.config import ROOT, load_config
from screener.fetch import PriceStore


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="+")
    ap.add_argument("--cache", default=str(ROOT / "cache" / "prices.sqlite"))
    args = ap.parse_args()

    cfg = load_config()
    store = PriceStore(args.cache)
    hist = int(cfg["data"]["history_days"])
    bench = str(cfg["data"].get("benchmark", "SPY"))

    scores: dict[str, float] = {}
    for sym in store.last_dates():
        df = store.load(sym, hist)
        if df.empty:
            continue
        scores[sym] = ind.rs_score(df["close"])
    ratings = ind.rs_ratings(scores)
    field = sorted(v for v in scores.values() if np.isfinite(v))
    print(f"field ranked: {len(field)} symbols with a full year of history")

    bdf = store.load(bench, hist)
    bc = bdf["close"].to_numpy(dtype=float) if not bdf.empty else None

    for sym in args.symbols:
        df = store.load(sym, hist)
        print(f"\n=== {sym} ===")
        if df.empty:
            print("  no data")
            continue
        c = df["close"].dropna().to_numpy(dtype=float)
        print(f"  bars {len(c)}   last {c[-1]:.2f} on {df.index[-1].date()}")
        if len(c) < 253:
            print("  under 253 bars - no rating")
            continue
        for lag, label in ((63, "3m"), (126, "6m"), (189, "9m"), (252, "12m")):
            past = c[-1 - lag]
            stock = (c[-1] / past - 1.0) * 100.0
            line = f"  {label:>3}: {past:9.2f} -> {c[-1]:8.2f}  {stock:+7.1f}%"
            if bc is not None and len(bc) > lag:
                b = (bc[-1] / bc[-1 - lag] - 1.0) * 100.0
                line += f"   {bench} {b:+6.1f}%   vs {bench} {stock - b:+7.1f}pp"
            print(line)
        sc = scores.get(sym, float("nan"))
        print(f"  raw score {sc:.4f}   ->  RS Rating {ratings.get(sym)}")
        if np.isfinite(sc) and field:
            below = sum(1 for v in field if v < sc)
            print(f"  beats {below}/{len(field)} = {below / len(field) * 100:.1f}% of the field")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
