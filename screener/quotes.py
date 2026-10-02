"""Intraday last-sale quotes for the names already on the published lists.

The VCP itself cannot move during the session: the base, pivot, support and
contractions all come from completed daily bars and are fixed until tonight's
close. What does move is where price sits against the pivot - still in the buy
zone, or already through it and missed - so only that is refreshed intraday,
for the hundred-odd published names rather than the whole universe.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import requests

from .fetch import UA, RateLimited

log = logging.getLogger(__name__)


def _clean(value) -> float:
    """Nasdaq returns prices as '$82.22' / 'N/A' / '1,234.50'."""
    if value is None:
        return float("nan")
    text = str(value).replace("$", "").replace(",", "").strip()
    if not text or text.upper() in ("N/A", "--"):
        return float("nan")
    try:
        return float(text)
    except ValueError:
        return float("nan")


def fetch_quote(session: requests.Session, symbol: str, timeout: int = 10) -> dict | None:
    """Last sale for one symbol, or None if it cannot be read."""
    for asset_class in ("stocks", "etf"):
        url = f"https://api.nasdaq.com/api/quote/{symbol}/info?assetclass={asset_class}"
        try:
            resp = session.get(
                url, timeout=timeout,
                headers={"User-Agent": UA, "Accept": "application/json"},
            )
        except requests.RequestException as exc:
            log.debug("%s quote failed: %s", symbol, exc)
            return None
        if resp.status_code in (429, 403):
            raise RateLimited(f"nasdaq {resp.status_code}")
        if resp.status_code != 200:
            continue
        data = (resp.json() or {}).get("data") or {}
        primary = data.get("primaryData") or {}
        price = _clean(primary.get("lastSalePrice"))
        if price != price:                      # NaN: wrong asset class, try the next
            continue
        pct = str(primary.get("percentageChange") or "").replace("%", "").strip()
        try:
            change_pct = float(pct)
        except ValueError:
            change_pct = float("nan")
        return {
            "price": round(price, 4),
            "change_pct": round(change_pct, 2) if change_pct == change_pct else None,
            # Nasdaq's own wording for how fresh this is, e.g. "LAST SALE" or
            # a delayed-quote notice. Reported rather than interpreted.
            "as_of": (primary.get("lastTradeTimestamp") or "").strip() or None,
            "delayed": bool(data.get("isRealTime") is False),
        }
    return None


def fetch_quotes(symbols: list[str], max_workers: int = 6) -> dict[str, dict]:
    """Quotes for many symbols. A symbol that fails is simply absent."""
    out: dict[str, dict] = {}
    with requests.Session() as session:
        def one(sym: str):
            try:
                return sym, fetch_quote(session, sym)
            except RateLimited:
                raise
            except Exception as exc:            # noqa: BLE001 - one bad symbol is not fatal
                log.debug("%s quote error: %s", sym, exc)
                return sym, None

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for sym, quote in pool.map(one, symbols):
                if quote:
                    out[sym] = quote
    return out
