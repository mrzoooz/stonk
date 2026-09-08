"""Daily-bar download with an on-disk cache.

Two free, keyless providers are used in order (Stooq, then Yahoo). Bars land in
a SQLite file so a nightly run only has to pull the handful of sessions it is
missing; a full backfill happens once, or whenever the CI cache is evicted.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    open   REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS meta (
    symbol     TEXT PRIMARY KEY,
    last_date  TEXT,
    fetched_at TEXT,
    status     TEXT
);
"""


class RateLimited(Exception):
    """Raised when a provider signals we have hit its limit."""


# --------------------------------------------------------------------------
# Providers
#
# Both keyless sources that survive a datacenter IP are used here. Stooq and
# Yahoo were tried first and are not viable from CI: Stooq answers every
# request with a JavaScript browser challenge (on .com and .pl alike) and
# Yahoo returns 429 for GitHub's IP ranges regardless of headers or cookies.
# --------------------------------------------------------------------------
def _clean_number(value: str) -> float:
    """Turn Nasdaq's display strings ('$319.97', '39,606,880') into floats."""
    if value is None:
        raise ValueError("missing")
    text = str(value).strip().replace("$", "").replace(",", "")
    if not text or text.upper() in {"N/A", "--"}:
        raise ValueError(f"not a number: {value!r}")
    return float(text)


def _nasdaq_request(
    session: requests.Session, symbol: str, start: date, asset_class: str, timeout: int
) -> list[dict]:
    url = (
        f"https://api.nasdaq.com/api/quote/{symbol}/historical"
        f"?assetclass={asset_class}&fromdate={start:%Y-%m-%d}"
        f"&todate={date.today():%Y-%m-%d}&limit=99999"
    )
    resp = session.get(
        url, timeout=timeout, headers={"User-Agent": UA, "Accept": "application/json"}
    )
    if resp.status_code == 404:
        return []
    if resp.status_code in (429, 403):
        raise RateLimited(f"nasdaq {resp.status_code}")
    resp.raise_for_status()
    payload = resp.json()
    node = payload.get("data") or {}
    table = node.get("tradesTable") or {}
    return table.get("rows") or []


def fetch_nasdaq(
    session: requests.Session, symbol: str, start: date, timeout: int
) -> pd.DataFrame:
    """Daily bars from Nasdaq's public quote API.

    The endpoint splits its universe by asset class and returns an empty table
    rather than an error for the wrong one, so an empty result is retried as an
    ETF. That is what the SPY benchmark needs.
    """
    rows = _nasdaq_request(session, symbol, start, "stocks", timeout)
    if not rows:
        rows = _nasdaq_request(session, symbol, start, "etf", timeout)
    return _nasdaq_frame(rows)


def _nasdaq_frame(rows: list[dict]) -> pd.DataFrame:
    recs = []
    for row in rows:
        try:
            month, day, year = str(row["date"]).split("/")
            recs.append(
                {
                    "date": f"{year}-{month}-{day}",
                    "open": _clean_number(row.get("open")),
                    "high": _clean_number(row.get("high")),
                    "low": _clean_number(row.get("low")),
                    "close": _clean_number(row.get("close")),
                    "volume": _clean_number(row.get("volume")),
                }
            )
        except (ValueError, KeyError, TypeError):
            # A single unparseable session must not lose the whole symbol.
            continue
    if not recs:
        return pd.DataFrame()
    # Nasdaq returns newest first; the rest of the screener expects ascending.
    return pd.DataFrame(recs).iloc[::-1].reset_index(drop=True)


def fetch_stockanalysis(
    session: requests.Session, symbol: str, start: date, timeout: int
) -> pd.DataFrame:
    """Fallback source. Its default window is about six months, which is not
    enough to seed a 200-day average but is ample for a nightly top-up."""
    url = f"https://stockanalysis.com/api/symbol/s/{symbol.lower()}/history"
    resp = session.get(url, timeout=timeout, headers={"User-Agent": UA, "Accept": "application/json"})
    if resp.status_code == 404:
        return pd.DataFrame()
    if resp.status_code in (429, 403):
        raise RateLimited(f"stockanalysis {resp.status_code}")
    resp.raise_for_status()
    node = resp.json().get("data")
    rows = node.get("data") if isinstance(node, dict) else node
    if not isinstance(rows, list):
        return pd.DataFrame()
    recs = []
    for row in rows:
        try:
            recs.append(
                {
                    "date": str(row["t"]),
                    "open": float(row["o"]),
                    "high": float(row["h"]),
                    "low": float(row["l"]),
                    "close": float(row["c"]),
                    "volume": float(row.get("v") or 0),
                }
            )
        except (ValueError, KeyError, TypeError):
            continue
    if not recs:
        return pd.DataFrame()
    frame = pd.DataFrame(recs)
    return frame.sort_values("date").reset_index(drop=True)


PROVIDERS = {"nasdaq": fetch_nasdaq, "stockanalysis": fetch_stockanalysis}


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------
class PriceStore:
    """SQLite-backed store of daily bars."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._lock = threading.Lock()

    def last_date(self, symbol: str) -> str | None:
        cur = self.conn.execute("SELECT last_date FROM meta WHERE symbol = ?", (symbol,))
        row = cur.fetchone()
        return row[0] if row and row[0] else None

    def last_dates(self) -> dict[str, str]:
        cur = self.conn.execute("SELECT symbol, last_date FROM meta WHERE last_date IS NOT NULL")
        return dict(cur.fetchall())

    def upsert(self, symbol: str, frame: pd.DataFrame, status: str = "ok") -> None:
        with self._lock:
            if not frame.empty:
                rows = [
                    (symbol, r.date, r.open, r.high, r.low, r.close, r.volume)
                    for r in frame.itertuples(index=False)
                ]
                self.conn.executemany(
                    "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?)", rows
                )
                last = max(frame["date"])
            else:
                last = self.last_date(symbol)
            self.conn.execute(
                "INSERT OR REPLACE INTO meta VALUES (?,?,?,?)",
                (symbol, last, datetime.now(timezone.utc).isoformat(timespec="seconds"), status),
            )
            self.conn.commit()

    def load(self, symbol: str, history_days: int | None = None) -> pd.DataFrame:
        query = "SELECT date, open, high, low, close, volume FROM bars WHERE symbol = ? ORDER BY date"
        frame = pd.read_sql_query(query, self.conn, params=(symbol,))
        if frame.empty:
            return frame
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame.set_index("date").sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        # Drop rows that carry no usable price.
        frame = frame.dropna(subset=["open", "high", "low", "close"])
        if history_days:
            frame = frame.tail(history_days)
        return frame

    def prune(self, keep_days: int) -> None:
        cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
        with self._lock:
            self.conn.execute("DELETE FROM bars WHERE date < ?", (cutoff,))
            self.conn.commit()

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")

    def close(self) -> None:
        self.conn.close()


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def _fetch_one(
    symbol: str,
    start: date,
    providers: list[str],
    disabled: set[str],
    timeout: int,
    retries: int,
    session: requests.Session,
) -> tuple[str, pd.DataFrame, str]:
    last_err = "no provider"
    for name in providers:
        if name in disabled:
            continue
        fn = PROVIDERS[name]
        for attempt in range(retries):
            try:
                frame = fn(session, symbol, start, timeout)
                if not frame.empty:
                    return symbol, frame, "ok"
                last_err = f"{name}: empty"
                break
            except RateLimited as exc:
                disabled.add(name)
                last_err = f"{name}: {exc}"
                break
            except Exception as exc:  # noqa: BLE001 - retry then fall to next provider
                last_err = f"{name}: {type(exc).__name__}"
                time.sleep(min(2 ** attempt * 0.5, 8))
    return symbol, pd.DataFrame(), last_err


def update_prices(
    symbols: list[str],
    store: PriceStore,
    cfg: dict,
    force_full: bool = False,
    progress_every: int = 250,
) -> dict[str, int]:
    """Bring every symbol in `symbols` up to date in the store."""
    dcfg = cfg.get("data", {})
    history_days = int(dcfg.get("history_days", 800))
    providers = [p for p in dcfg.get("providers", ["nasdaq", "stockanalysis"]) if p in PROVIDERS]
    workers = int(dcfg.get("max_workers", 8))
    timeout = int(dcfg.get("request_timeout", 20))
    retries = int(dcfg.get("retries", 3))

    known = store.last_dates()
    full_start = date.today() - timedelta(days=history_days)
    # Anything already current within a few days only needs a short top-up.
    incremental_start = date.today() - timedelta(days=20)

    jobs: list[tuple[str, date]] = []
    for sym in symbols:
        last = known.get(sym)
        if force_full or not last:
            jobs.append((sym, full_start))
            continue
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d").date()
        except ValueError:
            jobs.append((sym, full_start))
            continue
        if (date.today() - last_dt).days <= 15:
            jobs.append((sym, incremental_start))
        else:
            jobs.append((sym, full_start))

    disabled: set[str] = set()
    stats = {"ok": 0, "empty": 0, "total": len(jobs)}
    session = requests.Session()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_fetch_one, sym, start, providers, disabled, timeout, retries, session): sym
            for sym, start in jobs
        }
        for i, fut in enumerate(as_completed(futures), start=1):
            sym, frame, status = fut.result()
            store.upsert(sym, frame, status)
            stats["ok" if status == "ok" else "empty"] += 1
            if progress_every and i % progress_every == 0:
                log.info(
                    "fetched %d/%d (ok=%d empty=%d, disabled=%s)",
                    i, stats["total"], stats["ok"], stats["empty"], sorted(disabled) or "-",
                )
    if disabled:
        log.warning("providers disabled during this run: %s", sorted(disabled))
    return stats
