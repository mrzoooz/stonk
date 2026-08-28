"""Daily-bar download with an on-disk cache.

Two free, keyless providers are used in order (Stooq, then Yahoo). Bars land in
a SQLite file so a nightly run only has to pull the handful of sessions it is
missing; a full backfill happens once, or whenever the CI cache is evicted.
"""
from __future__ import annotations

import csv
import io
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
# --------------------------------------------------------------------------
def _stooq_symbol(symbol: str) -> str:
    return symbol.replace(".", "-").replace("/", "-").lower() + ".us"


def fetch_stooq(session: requests.Session, symbol: str, start: date, timeout: int) -> pd.DataFrame:
    url = (
        "https://stooq.com/q/d/l/?s="
        f"{_stooq_symbol(symbol)}&d1={start:%Y%m%d}&d2={date.today():%Y%m%d}&i=d"
    )
    resp = session.get(url, timeout=timeout, headers={"User-Agent": UA})
    resp.raise_for_status()
    text = resp.text.strip()
    if not text or text.lower().startswith("no data"):
        return pd.DataFrame()
    if "exceeded" in text[:200].lower():
        raise RateLimited("stooq daily hits limit")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "Close" not in rows[0]:
        return pd.DataFrame()
    recs = []
    for r in rows:
        try:
            recs.append(
                {
                    "date": r["Date"],
                    "open": float(r["Open"]),
                    "high": float(r["High"]),
                    "low": float(r["Low"]),
                    "close": float(r["Close"]),
                    "volume": float(r.get("Volume") or 0),
                }
            )
        except (ValueError, KeyError, TypeError):
            continue
    return pd.DataFrame(recs)


def _yahoo_symbol(symbol: str) -> str:
    return symbol.replace(".", "-").upper()


def fetch_yahoo(session: requests.Session, symbol: str, start: date, timeout: int) -> pd.DataFrame:
    period1 = int(datetime.combine(start, datetime.min.time()).timestamp())
    period2 = int(time.time()) + 86400
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{_yahoo_symbol(symbol)}"
        f"?period1={period1}&period2={period2}&interval=1d&events=div%2Csplit"
    )
    resp = session.get(url, timeout=timeout, headers={"User-Agent": UA})
    if resp.status_code in (429, 999):
        raise RateLimited(f"yahoo {resp.status_code}")
    if resp.status_code == 404:
        return pd.DataFrame()
    resp.raise_for_status()
    payload = resp.json()
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        return pd.DataFrame()
    node = result[0]
    stamps = node.get("timestamp") or []
    quote = ((node.get("indicators") or {}).get("quote") or [{}])[0]
    if not stamps or not quote:
        return pd.DataFrame()
    frame = pd.DataFrame(
        {
            "date": [datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d") for t in stamps],
            "open": quote.get("open"),
            "high": quote.get("high"),
            "low": quote.get("low"),
            "close": quote.get("close"),
            "volume": quote.get("volume"),
        }
    )
    return frame.dropna(subset=["open", "high", "low", "close"])


PROVIDERS = {"stooq": fetch_stooq, "yahoo": fetch_yahoo}


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
    providers = [p for p in dcfg.get("providers", ["stooq", "yahoo"]) if p in PROVIDERS]
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
