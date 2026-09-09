"""Is there an authoritative free source for split dates?

Inferring splits from price and volume cannot separate a real 2:1 split from a
stock that halved on bad news - both move price by the same factor on elevated
volume. If a source states splits directly, or serves an adjusted close, the
guessing stops.

PRIM (2026-05-06, 202.92 -> 101.23) and WVE (2026-03-26, 12.30 -> 6.20) both
look identical to the heuristic; one is very likely a split, the other very
likely a crash.
"""
from __future__ import annotations

import json

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
H = {"User-Agent": UA, "Accept": "application/json"}


def show(label, fn):
    print("=" * 72)
    print(label)
    try:
        print("  ", fn())
    except Exception as exc:  # noqa: BLE001
        print(f"   EXCEPTION {type(exc).__name__}: {exc}")


def get(url):
    r = requests.get(url, headers=H, timeout=25)
    return r.status_code, r.text[:400]


def sa_history(sym, period=""):
    """stockanalysis returns both close (c) and adjusted close (a)."""
    url = f"https://stockanalysis.com/api/symbol/s/{sym.lower()}/history"
    if period:
        url += f"?range={period}"
    r = requests.get(url, headers=H, timeout=25)
    if r.status_code != 200:
        return f"HTTP {r.status_code}"
    node = r.json().get("data")
    rows = node.get("data") if isinstance(node, dict) else node
    if not isinstance(rows, list) or not rows:
        return "no rows"
    # If a != c anywhere, the feed carries a split/dividend adjustment factor.
    diff = [r0 for r0 in rows if r0.get("a") and r0.get("c")
            and abs(r0["a"] / r0["c"] - 1) > 0.01]
    return (f"{len(rows)} rows, {rows[-1]['t']} .. {rows[0]['t']}, "
            f"rows where adjusted != close: {len(diff)}"
            + (f", e.g. {diff[0]}" if diff else ""))


for sym in ("PRIM", "WVE"):
    show(f"stockanalysis history, default range - {sym}", lambda s=sym: sa_history(s))
    for rng in ("1Y", "2Y", "5Y"):
        show(f"stockanalysis history range={rng} - {sym}",
             lambda s=sym, r=rng: sa_history(s, r))

show("nasdaq dividends endpoint (does it carry splits?) - PRIM",
     lambda: get("https://api.nasdaq.com/api/quote/PRIM/dividends?assetclass=stocks"))
show("nasdaq corporate actions - PRIM",
     lambda: get("https://api.nasdaq.com/api/company/PRIM/corporate-actions"))
show("stockanalysis splits page json - PRIM",
     lambda: get("https://stockanalysis.com/api/symbol/s/prim/splits"))


# ---------------------------------------------------------------------------
# Decisive check: put the two sources side by side across a step date.
#
# If stockanalysis shows a smooth series where Nasdaq shows a 2:1 step, then
# stockanalysis's close is already split-adjusted and switching to it removes
# the whole problem. If both step, the move is real and PRIM genuinely halved.
# ---------------------------------------------------------------------------
def sa_around(sym, target, span=3):
    url = f"https://stockanalysis.com/api/symbol/s/{sym.lower()}/history?range=5Y"
    r = requests.get(url, headers=H, timeout=25)
    node = r.json().get("data")
    rows = node.get("data") if isinstance(node, dict) else node
    rows = sorted(rows, key=lambda x: x["t"])
    idx = [i for i, x in enumerate(rows) if x["t"] >= target]
    if not idx:
        return "date not in range"
    i = idx[0]
    window = rows[max(0, i - span):i + span]
    return " | ".join(f"{x['t']} c={x['c']} a={x['a']}" for x in window)


for sym, date_ in (("PRIM", "2026-05-06"), ("WVE", "2026-03-26"),
                   ("ANPA", "2026-02-13"), ("HTCO", "2025-03-24")):
    show(f"stockanalysis around {date_} - {sym}", lambda s=sym, d=date_: sa_around(s, d))
