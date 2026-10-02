"""Refresh last-sale prices for the published names and write state/quotes.json.

Deliberately does not re-run the screen. The pattern is fixed until tonight's
close; only the price against the pivot moves, and that is all this updates.

The file is committed to the repo rather than deployed to Pages: Pages has a
soft limit of about ten builds an hour, which a five-minute refresh would
blow through, and raw.githubusercontent.com serves with permissive CORS so
the app can read it directly.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from screener.config import ROOT, load_config
from screener.quotes import fetch_quotes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(ROOT / "docs" / "data" / "screen.json"))
    ap.add_argument("--out", default=str(ROOT / "state" / "quotes.json"))
    ap.add_argument("--max-symbols", type=int, default=200)
    args = ap.parse_args()

    results = Path(args.results)
    if not results.exists():
        print("no screen.json - nothing to refresh")
        return 0
    data = json.loads(results.read_text())
    cfg = load_config()
    tol = float(cfg["vcp"].get("pivot_break_tolerance_pct", 0.0)) / 100.0

    # Only names with a pivot: a Stage 2 row with no base has nothing intraday
    # worth watching.
    levels: dict[str, dict] = {}
    for bucket in ("ready", "watch"):
        for row in data.get(bucket, []):
            m = (row.get("vcp") or {}).get("metrics") or {}
            if m.get("pivot") is None:
                continue
            levels[row["symbol"]] = {
                "pivot": m["pivot"], "support": m.get("support"),
                "stop": m.get("stop"), "bucket": bucket,
                "close": row.get("price"),
            }
    symbols = list(levels)[: args.max_symbols]
    print(f"refreshing {len(symbols)} symbols")

    quotes = fetch_quotes(symbols)
    print(f"got {len(quotes)} quotes")

    out: dict[str, dict] = {}
    for sym, q in quotes.items():
        lv = levels[sym]
        pivot = float(lv["pivot"])
        price = float(q["price"])
        dist = (price - pivot) / pivot * 100.0 if pivot > 0 else None
        row = {
            "price": price,
            "change_pct": q.get("change_pct"),
            "dist_to_pivot_pct": round(dist, 2) if dist is not None else None,
            # Last sale only: the quote carries no intraday high, so a spike
            # through the pivot and back between two samples is not seen here.
            "through_pivot": pivot > 0 and price > pivot * (1.0 + tol),
            "below_stop": lv.get("stop") is not None and price < float(lv["stop"]),
        }
        if q.get("as_of"):
            row["quote_time"] = q["as_of"]
        out[sym] = row

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "screen_as_of": data.get("as_of"),
        "count": len(out),
        "quotes": out,
    }
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, separators=(",", ":")))
    through = sum(1 for r in out.values() if r["through_pivot"])
    print(f"wrote {dest}  ({through} now above their pivot)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
