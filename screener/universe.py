"""Building the list of US common stocks to scan.

Primary source is the NASDAQ Trader symbol directory (free, no key, updated
nightly). The SEC company-ticker file is used as a fallback, and a snapshot
cached from a previous run is the last resort so a bad night for one host does
not take the whole screen down.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path

import requests

log = logging.getLogger(__name__)

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"

UA = "stonk-screener/1.0 (+https://github.com/mrzoooz/stonk)"

# Exchange letter codes used in otherlisted.txt.
EXCHANGE_CODES = {
    "A": "NYSE MKT",
    "N": "NYSE",
    "P": "NYSE ARCA",
    "Z": "BATS",
    "V": "IEX",
}

# Suffixes that mark something other than common stock.
NON_COMMON_SUFFIXES = ("W", "R", "U", "P")


def _get(url: str, timeout: int = 30) -> str:
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
    resp.raise_for_status()
    return resp.text


def _is_common_stock(symbol: str, name: str, nasdaq: bool) -> bool:
    """Filter out warrants, units, rights, preferreds and test issues."""
    if not symbol or any(ch in symbol for ch in "$^"):
        return False
    lowered = name.lower()
    for bad in (
        " warrant",
        " right",
        " unit",
        "preferred",
        "depositary",
        " notes",
        " debenture",
        "% due",
    ):
        if bad in lowered:
            return False
    if "." in symbol:
        # Class shares (BRK.B) are fine; anything longer is a preferred series.
        head, _, tail = symbol.partition(".")
        if len(tail) != 1 or not tail.isalpha():
            return False
    elif nasdaq and len(symbol) == 5 and symbol[-1] in NON_COMMON_SUFFIXES:
        return False
    return True


def _parse_nasdaq_listed(text: str) -> list[dict]:
    out = []
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    for row in reader:
        sym = (row.get("Symbol") or "").strip()
        if not sym or sym.startswith("File Creation Time"):
            continue
        out.append(
            {
                "symbol": sym,
                "name": (row.get("Security Name") or "").strip(),
                "exchange": "NASDAQ",
                "etf": (row.get("ETF") or "N").strip().upper() == "Y",
                "test": (row.get("Test Issue") or "N").strip().upper() == "Y",
                "nasdaq": True,
            }
        )
    return out


def _parse_other_listed(text: str) -> list[dict]:
    out = []
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    for row in reader:
        sym = (row.get("ACT Symbol") or "").strip()
        if not sym or sym.startswith("File Creation Time"):
            continue
        out.append(
            {
                "symbol": sym,
                "name": (row.get("Security Name") or "").strip(),
                "exchange": EXCHANGE_CODES.get((row.get("Exchange") or "").strip(), "OTHER"),
                "etf": (row.get("ETF") or "N").strip().upper() == "Y",
                "test": (row.get("Test Issue") or "N").strip().upper() == "Y",
                "nasdaq": False,
            }
        )
    return out


def _from_sec() -> list[dict]:
    data = json.loads(_get(SEC_TICKERS))
    rows = data.values() if isinstance(data, dict) else data
    return [
        {
            "symbol": str(r["ticker"]).strip().upper(),
            "name": str(r.get("title", "")).strip(),
            "exchange": "UNKNOWN",
            "etf": False,
            "test": False,
            "nasdaq": False,
        }
        for r in rows
        if r.get("ticker")
    ]


def build_universe(cfg: dict, cache_path: Path | None = None) -> list[dict]:
    """Return [{symbol, name, exchange}, ...] after applying the config filters."""
    ucfg = cfg.get("universe", {})
    rows: list[dict] = []

    try:
        rows = _parse_nasdaq_listed(_get(NASDAQ_LISTED)) + _parse_other_listed(_get(OTHER_LISTED))
        log.info("universe: %d rows from nasdaqtrader", len(rows))
    except Exception as exc:  # noqa: BLE001 - any failure falls through
        log.warning("nasdaqtrader unavailable (%s), trying SEC", exc)
        try:
            rows = _from_sec()
            log.info("universe: %d rows from SEC", len(rows))
        except Exception as exc2:  # noqa: BLE001
            log.warning("SEC unavailable (%s)", exc2)

    if not rows and cache_path and Path(cache_path).exists():
        rows = json.loads(Path(cache_path).read_text())
        log.warning("universe: falling back to cached snapshot (%d rows)", len(rows))

    if not rows:
        raise RuntimeError("could not build a symbol universe from any source")

    allowed = {e.upper() for e in ucfg.get("exchanges", [])}
    out, seen = [], set()
    for r in rows:
        sym = r["symbol"].strip().upper()
        if sym in seen:
            continue
        if ucfg.get("exclude_test_issues", True) and r.get("test"):
            continue
        if ucfg.get("exclude_etfs", True) and r.get("etf"):
            continue
        if ucfg.get("exclude_suffixed", True) and not _is_common_stock(
            sym, r.get("name", ""), r.get("nasdaq", False)
        ):
            continue
        if allowed and r["exchange"] != "UNKNOWN" and r["exchange"].upper() not in allowed:
            continue
        seen.add(sym)
        out.append({"symbol": sym, "name": r.get("name", ""), "exchange": r["exchange"]})

    out.sort(key=lambda r: r["symbol"])
    limit = int(ucfg.get("limit", 0) or 0)
    if limit:
        out = out[:limit]

    if cache_path:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cache_path).write_text(json.dumps(out))
    return out
