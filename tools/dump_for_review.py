"""Dump a compact structural summary of trend-leading names for manual review.

Deliberately does NOT apply the VCP gates. The point is to hand over the raw
shape of each base - every swing high and low, with volume - so the pattern
call is made by whoever reads this, not by vcp.py. The net is cast with the
moving-average stack and RS alone, which are not in question.

Swing extraction here is plain data reduction (local extrema over a window),
not the suffix-minimum walk the screener uses, so the structure is not
pre-filtered by the rules under review.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from screener import indicators as ind
from screener.config import ROOT, load_config
from screener.fetch import PriceStore
from screener.universe import build_universe


def swings(highs, lows, vols, dates, span: int = 5) -> list[dict]:
    """Local extrema: a bar whose high (low) leads its neighbours either side."""
    out = []
    n = len(highs)
    for i in range(span, n - span):
        window = slice(i - span, i + span + 1)
        if highs[i] == np.max(highs[window]):
            out.append({"d": dates[i], "k": "H", "p": round(float(highs[i]), 2),
                        "v": int(vols[i])})
        elif lows[i] == np.min(lows[window]):
            out.append({"d": dates[i], "k": "L", "p": round(float(lows[i]), 2),
                        "v": int(vols[i])})
    # Collapse runs of the same kind, keeping the most extreme.
    merged: list[dict] = []
    for s in out:
        if merged and merged[-1]["k"] == s["k"]:
            better = s["p"] > merged[-1]["p"] if s["k"] == "H" else s["p"] < merged[-1]["p"]
            if better:
                merged[-1] = s
        else:
            merged.append(s)
    return merged


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(ROOT / "cache" / "prices.sqlite"))
    ap.add_argument("--out", default=str(ROOT / "state" / "review.json"))
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--bars", type=int, default=160)
    args = ap.parse_args()

    cfg = load_config()
    store = PriceStore(args.cache)
    hist = int(cfg["data"]["history_days"])
    s2 = cfg["stage2"]
    n_s, n_m, n_l = int(s2["ma_short"]), int(s2["ma_mid"]), int(s2["ma_long"])

    bench = store.load(str(cfg["data"].get("benchmark", "SPY")), hist)
    bench_close = bench["close"] if not bench.empty else None

    meta = {m["symbol"]: m for m in build_universe(cfg, ROOT / "cache" / "universe.json")}
    scores: dict[str, float] = {}
    frames: dict[str, object] = {}
    for sym in store.last_dates():
        df = store.load(sym, hist)
        if len(df) < 260:
            continue
        frames[sym] = df
        scores[sym] = ind.rs_score(df["close"])
    ratings = ind.rs_ratings(scores)

    rows = []
    for sym, df in frames.items():
        close = df["close"]
        ma_s, ma_m, ma_l = ind.sma(close, n_s), ind.sma(close, n_m), ind.sma(close, n_l)
        price = float(close.iloc[-1])
        a, b, c = float(ma_s.iloc[-1]), float(ma_m.iloc[-1]), float(ma_l.iloc[-1])
        if not (np.isfinite(a) and np.isfinite(b) and np.isfinite(c)):
            continue
        # The net: the stack, price above it, and a real listing. Nothing else.
        if not (price > a > b > c):
            continue
        if price < float(cfg["universe"].get("min_price", 5)):
            continue
        rating = ratings.get(sym)
        if rating is None or rating < 70:
            continue
        rows.append((rating, sym, df, ma_s, ma_m, ma_l, price, a, b, c))

    rows.sort(key=lambda r: -r[0])
    rows = rows[: args.top]

    out = []
    for rating, sym, df, ma_s, ma_m, ma_l, price, a, b, c in rows:
        tail = df.tail(args.bars)
        highs = tail["high"].to_numpy(float)
        lows = tail["low"].to_numpy(float)
        vols = tail["volume"].to_numpy(float)
        dates = [d.strftime("%m-%d") for d in tail.index]
        avg50 = float(np.mean(df["volume"].to_numpy(float)[-50:]))
        stack = (ma_s > ma_m) & (ma_m > ma_l)
        lo52, hi52 = ind.week52(df)
        out.append({
            "sym": sym,
            "name": (meta.get(sym, {}).get("name") or "")[:28],
            "px": round(price, 2),
            "rs": rating,
            "ma": [round(a, 2), round(b, 2), round(c, 2)],
            "stack_age": ind.trailing_streak(stack),
            "off_high_pct": round(ind.pct_from(price, hi52), 1),
            "avg_vol_50d": int(avg50),
            "rs_63d": round(ind.relative_strength(df["close"], bench_close, 63), 1)
                      if bench_close is not None else None,
            "swings": swings(highs, lows, vols, dates),
        })

    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({"as_of": str(df.index[-1].date()), "names": out},
                               separators=(",", ":")))
    print(f"wrote {len(out)} names to {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
