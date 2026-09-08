"""Answer the implementation questions for the two keyless sources that work.

Round 2 showed api.nasdaq.com and stockanalysis.com both return daily OHLCV
without a key from a runner. Before rewriting the fetcher we need to know how
much history each gives, how they spell class shares and ETFs, and whether they
hold up under the concurrency a 6,000-symbol scan needs.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
H = {"User-Agent": UA, "Accept": "application/json"}


def nasdaq_url(sym: str, frm: str = "2024-09-01", to: str = "2026-09-08") -> str:
    return (f"https://api.nasdaq.com/api/quote/{sym}/historical"
            f"?assetclass=stocks&fromdate={frm}&todate={to}&limit=99999")


def nasdaq_rows(sym: str, **kw):
    r = requests.get(nasdaq_url(sym, **kw), headers=H, timeout=25)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    try:
        d = r.json()
    except Exception:
        return None, f"non-JSON: {r.text[:80]!r}"
    node = (d.get("data") or {})
    if not node:
        return None, f"no data node; status={d.get('status', {}).get('rCode')}"
    table = node.get("tradesTable") or {}
    rows = table.get("rows") or []
    return rows, f"{node.get('totalRecords')} records"


def sa_rows(sym: str, period: str = ""):
    url = f"https://stockanalysis.com/api/symbol/s/{sym.lower()}/history"
    if period:
        url += f"?range={period}"
    r = requests.get(url, headers=H, timeout=25)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    d = r.json()
    rows = ((d.get("data") or {}).get("data")) or []
    return rows, f"{len(rows)} rows"


print("=" * 72)
print("A. nasdaq history depth (asked for 2 years)")
rows, note = nasdaq_rows("AAPL")
print("  ", note)
if rows:
    print("   newest:", rows[0]["date"], "| oldest:", rows[-1]["date"], "| count:", len(rows))
    print("   sample:", json.dumps(rows[0]))

print("=" * 72)
print("B. nasdaq: ETF (SPY benchmark) - note assetclass=stocks vs etf")
for ac in ("stocks", "etf"):
    r = requests.get(
        f"https://api.nasdaq.com/api/quote/SPY/historical"
        f"?assetclass={ac}&fromdate=2026-08-01&todate=2026-09-08&limit=999",
        headers=H, timeout=25)
    try:
        n = len((((r.json().get("data") or {}).get("tradesTable") or {}).get("rows")) or [])
    except Exception:
        n = "parse-fail"
    print(f"   assetclass={ac}: HTTP {r.status_code}, rows={n}")

print("=" * 72)
print("C. nasdaq: class shares / dotted symbols")
for sym in ("BRK/B", "BRK.B", "BRK-B", "BF/B"):
    rows, note = nasdaq_rows(sym, frm="2026-08-01")
    print(f"   {sym:6s} -> {note}")

print("=" * 72)
print("D. nasdaq under concurrency (30 symbols, 6 workers)")
syms = ("AAPL MSFT NVDA AMZN GOOG META TSLA AVGO JPM V UNH XOM MA JNJ PG HD "
        "COST ABBV MRK CVX ADBE CRM AMD NFLX PEP KO TMO WMT BAC ORCL").split()
t0 = time.time()
with ThreadPoolExecutor(max_workers=6) as ex:
    out = list(ex.map(lambda s: nasdaq_rows(s, frm="2026-06-01"), syms))
ok = sum(1 for rows, _ in out if rows)
print(f"   {ok}/{len(syms)} ok in {time.time() - t0:.1f}s")
for sym, (rows, note) in zip(syms, out):
    if not rows:
        print(f"   FAIL {sym}: {note}")

print("=" * 72)
print("E. stockanalysis history depth + ranges")
for period in ("", "1Y", "5Y", "10Y"):
    rows, note = sa_rows("AAPL", period)
    if rows:
        print(f"   range={period or '(default)':10s} {note}, newest={rows[0]['t']}, oldest={rows[-1]['t']}")
    else:
        print(f"   range={period or '(default)':10s} {note}")

print("=" * 72)
print("F. stockanalysis under concurrency (30 symbols, 6 workers)")
t0 = time.time()
with ThreadPoolExecutor(max_workers=6) as ex:
    out = list(ex.map(lambda s: sa_rows(s, "5Y"), syms))
ok = sum(1 for rows, _ in out if rows)
print(f"   {ok}/{len(syms)} ok in {time.time() - t0:.1f}s")
for sym, (rows, note) in zip(syms, out):
    if not rows:
        print(f"   FAIL {sym}: {note}")
