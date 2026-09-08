"""Probe free daily-bar sources from wherever this runs.

The screener's fetchers work fine locally but returned nothing from a GitHub
Actions runner. This prints exactly what each candidate source replies with, so
the choice of provider is made on evidence rather than assumption.
"""
from __future__ import annotations

import json
import time

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def show(label: str, fn) -> None:
    print("=" * 70)
    print(label)
    try:
        started = time.time()
        status, body = fn()
        print(f"  status={status}  {time.time() - started:.1f}s")
        print(f"  body[:300]={body[:300]!r}")
    except Exception as exc:  # noqa: BLE001 - a probe reports failures, never raises
        print(f"  EXCEPTION {type(exc).__name__}: {exc}")


def get(url: str, headers: dict | None = None, timeout: int = 20):
    r = requests.get(url, headers=headers or {"User-Agent": UA}, timeout=timeout)
    return r.status_code, r.text


def main() -> None:
    show("1. stooq CSV, with date range (what the screener sends today)",
         lambda: get("https://stooq.com/q/d/l/?s=aapl.us&d1=20240101&d2=20260908&i=d"))

    show("2. stooq CSV, no date range",
         lambda: get("https://stooq.com/q/d/l/?s=aapl.us&i=d"))

    show("3. stooq, plain UA",
         lambda: get("https://stooq.com/q/d/l/?s=aapl.us&i=d", {"User-Agent": "curl/8.0"}))

    show("4. yahoo chart v8",
         lambda: get("https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=1mo&interval=1d"))

    show("5. yahoo chart v8 via query2",
         lambda: get("https://query2.finance.yahoo.com/v8/finance/chart/AAPL?range=1mo&interval=1d"))

    def yahoo_with_cookie():
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        s.get("https://fc.yahoo.com", timeout=15)
        r = s.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=1mo&interval=1d",
            timeout=20,
        )
        return r.status_code, r.text
    show("6. yahoo chart after a cookie-priming request", yahoo_with_cookie)

    # A single bulk file would beat thousands of per-symbol requests outright.
    def stooq_bulk_head():
        r = requests.head("https://stooq.com/db/d/?b=d_us_txt", headers={"User-Agent": UA},
                          timeout=25, allow_redirects=True)
        return r.status_code, json.dumps(dict(r.headers))
    show("7. stooq bulk US daily zip (HEAD)", stooq_bulk_head)

    show("8. nasdaqtrader (known good, as a control)",
         lambda: get("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"))

    # Keyless alternatives worth knowing about.
    show("9. alpaca IEX daily bars, no key (expect 401/403 - proves reachability)",
         lambda: get("https://data.alpaca.markets/v2/stocks/bars?symbols=AAPL&timeframe=1Day&limit=5"))

    show("10. tiingo, no key (expect 401 - proves reachability)",
         lambda: get("https://api.tiingo.com/tiingo/daily/aapl/prices"))

    # --- round 2: is there ANY keyless source that works from here? --------

    show("11. stooq.pl (different domain, maybe no browser challenge)",
         lambda: get("https://stooq.pl/q/d/l/?s=aapl.us&i=d"))

    show("12. nasdaq.com historical JSON (same operator as the working control)",
         lambda: get("https://api.nasdaq.com/api/quote/AAPL/historical"
                     "?assetclass=stocks&fromdate=2025-09-01&todate=2026-09-08&limit=9999"))

    show("13. alphavantage demo key",
         lambda: get("https://www.alphavantage.co/query"
                     "?function=TIME_SERIES_DAILY&symbol=IBM&apikey=demo"))

    show("14. twelvedata demo",
         lambda: get("https://api.twelvedata.com/time_series"
                     "?symbol=AAPL&interval=1day&outputsize=5&apikey=demo"))

    show("15. eodhd demo token",
         lambda: get("https://eodhd.com/api/eod/AAPL.US?api_token=demo&fmt=json"))

    # The prize: one request returns every US ticker's bar for a whole day, so
    # a nightly update is a single call and a backfill is one call per session.
    show("16. polygon grouped daily, no key (expect 401 - proves reachability)",
         lambda: get("https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/2026-09-04"))

    show("17. stockanalysis.com",
         lambda: get("https://stockanalysis.com/api/symbol/s/AAPL/history"))


if __name__ == "__main__":
    main()
