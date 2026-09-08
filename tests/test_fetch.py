"""Parser tests for the two keyless daily-bar sources.

Payload shapes here are copied from what the sources actually returned from a
CI runner, so the parsers are checked against reality rather than a guess.
"""
import pandas as pd
import pytest

from screener import fetch


class FakeResponse:
    def __init__(self, payload=None, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Replays a queued list of responses and records the URLs requested."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self._responses.pop(0)


def nasdaq_payload(rows):
    return {"data": {"symbol": "X", "totalRecords": len(rows), "tradesTable": {"rows": rows}}}


NASDAQ_ROWS = [
    {"date": "09/04/2026", "close": "$319.97", "volume": "39,606,880",
     "open": "$328.305", "high": "$328.93", "low": "$317.86"},
    {"date": "09/03/2026", "close": "$328.21", "volume": "37,225,838",
     "open": "$324.87", "high": "$330.81", "low": "$324.11"},
]


def test_nasdaq_strips_currency_and_thousands_separators():
    session = FakeSession([FakeResponse(nasdaq_payload(NASDAQ_ROWS))])
    frame = fetch.fetch_nasdaq(session, "AAPL", pd.Timestamp("2026-08-01").date(), 10)
    assert list(frame.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert frame["close"].tolist() == [328.21, 319.97]
    assert frame["volume"].tolist() == [37225838.0, 39606880.0]
    assert frame["open"].tolist() == [324.87, 328.305]


def test_nasdaq_returns_ascending_dates():
    """The API sends newest first; everything downstream assumes oldest first."""
    session = FakeSession([FakeResponse(nasdaq_payload(NASDAQ_ROWS))])
    frame = fetch.fetch_nasdaq(session, "AAPL", pd.Timestamp("2026-08-01").date(), 10)
    assert frame["date"].tolist() == ["2026-09-03", "2026-09-04"]
    assert frame["date"].is_monotonic_increasing


def test_nasdaq_retries_an_empty_stock_result_as_an_etf():
    """SPY, the benchmark, only returns rows under assetclass=etf."""
    session = FakeSession([
        FakeResponse(nasdaq_payload([])),
        FakeResponse(nasdaq_payload(NASDAQ_ROWS)),
    ])
    frame = fetch.fetch_nasdaq(session, "SPY", pd.Timestamp("2026-08-01").date(), 10)
    assert len(frame) == 2
    assert "assetclass=stocks" in session.urls[0]
    assert "assetclass=etf" in session.urls[1]


def test_nasdaq_skips_unparseable_sessions_without_losing_the_symbol():
    rows = NASDAQ_ROWS + [
        {"date": "09/02/2026", "close": "N/A", "volume": "N/A",
         "open": "N/A", "high": "N/A", "low": "N/A"},
    ]
    session = FakeSession([FakeResponse(nasdaq_payload(rows))])
    frame = fetch.fetch_nasdaq(session, "AAPL", pd.Timestamp("2026-08-01").date(), 10)
    assert len(frame) == 2


def test_nasdaq_404_is_an_empty_frame_not_an_error():
    session = FakeSession([FakeResponse(status=404), FakeResponse(status=404)])
    assert fetch.fetch_nasdaq(session, "NOPE", pd.Timestamp("2026-08-01").date(), 10).empty


def test_nasdaq_429_raises_rate_limited_so_the_provider_is_disabled():
    session = FakeSession([FakeResponse(status=429)])
    with pytest.raises(fetch.RateLimited):
        fetch.fetch_nasdaq(session, "AAPL", pd.Timestamp("2026-08-01").date(), 10)


SA_ROWS = [
    {"a": 319.97, "c": 319.97, "h": 328.93, "l": 317.86, "o": 328.305,
     "t": "2026-09-04", "v": 39606884, "ch": -2.51},
    {"a": 328.21, "c": 328.21, "h": 330.81, "l": 324.11, "o": 324.87,
     "t": "2026-09-03", "v": 37225838, "ch": 1},
]


def test_stockanalysis_parses_and_sorts_ascending():
    session = FakeSession([FakeResponse({"status": 200, "data": {"data": SA_ROWS}})])
    frame = fetch.fetch_stockanalysis(session, "AAPL", pd.Timestamp("2026-08-01").date(), 10)
    assert frame["date"].tolist() == ["2026-09-03", "2026-09-04"]
    assert frame["close"].tolist() == [328.21, 319.97]


def test_stockanalysis_accepts_a_bare_list_payload():
    """The endpoint returns data as a list for some ranges and a dict for others."""
    session = FakeSession([FakeResponse({"status": 200, "data": SA_ROWS})])
    frame = fetch.fetch_stockanalysis(session, "AAPL", pd.Timestamp("2026-08-01").date(), 10)
    assert len(frame) == 2


def test_clean_number_rejects_placeholders():
    assert fetch._clean_number("$1,234.50") == 1234.5
    for bad in ("N/A", "--", "", None):
        with pytest.raises(ValueError):
            fetch._clean_number(bad)
