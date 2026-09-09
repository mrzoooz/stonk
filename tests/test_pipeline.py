import json

import pandas as pd
import pytest

from screener.fetch import PriceStore
from screener.run import evaluate_symbol, main
from tests.synth import _bars, benchmark_like, leg
from tests.test_vcp import build_base


def _seed(store: PriceStore, symbol: str, df: pd.DataFrame) -> None:
    frame = df.reset_index().rename(columns={"index": "date"})
    frame["date"] = frame["date"].dt.strftime("%Y-%m-%d")
    store.upsert(symbol, frame[["date", "open", "high", "low", "close", "volume"]])


@pytest.fixture
def screened(tmp_path):
    """Run the whole pipeline over three synthetic names and return the JSON."""
    store = PriceStore(tmp_path / "prices.sqlite")

    # Volume scaled so the $900M dollar-volume filter is satisfied.
    tight = build_base(t3_low=55.3)
    wide = build_base(t3_low=52.5, seed=5)
    for df in (tight, wide):
        df["volume"] *= 40
    down_closes = leg(100, 40, 380) + [40.0]
    down = _bars(down_closes, [120_000_000] * len(down_closes), seed=7)

    _seed(store, "TIGHT", tight)
    _seed(store, "WIDE", wide)
    _seed(store, "DOWN", down)

    bench = benchmark_like(tight["close"])
    _seed(
        store,
        "SPY",
        pd.DataFrame(
            {"open": bench, "high": bench * 1.004, "low": bench * 0.996,
             "close": bench, "volume": [1e8] * len(bench)},
            index=bench.index,
        ),
    )
    store.close()

    out = tmp_path / "out"
    assert main([
        "--no-fetch",
        "--cache", str(tmp_path / "prices.sqlite"),
        "--out", str(out),
        "--symbols", "TIGHT,WIDE,DOWN",
    ]) == 0
    return json.loads((out / "screen.json").read_text()), out


def test_buckets_split_by_hard_rules(screened):
    data, _ = screened
    assert [r["symbol"] for r in data["ready"]] == ["TIGHT"]
    assert [r["symbol"] for r in data["watch"]] == ["WIDE"]
    # The downtrend never reaches the VCP stage - Stage 2 rejects it outright.
    assert "DOWN" not in {r["symbol"] for r in data["ready"] + data["watch"]}
    assert "DOWN" not in {r["symbol"] for r in data["stage2"]}


def test_ready_row_carries_the_trade_levels(screened):
    data, _ = screened
    row = data["ready"][0]
    m = row["vcp"]["metrics"]
    assert m["support"] < m["pivot"] < m["target"]
    assert m["risk_pct"] <= data["config"]["max_risk_pct"]
    assert m["reward_risk"] >= data["config"]["min_reward_risk"]
    assert len(row["vcp"]["contractions"]) == m["contraction_count"]
    assert all(c["passed"] for c in row["stage2"]["checks"])


def test_watch_row_explains_why_it_missed(screened):
    data, _ = screened
    assert "risk" in data["watch"][0]["vcp"]["reason"]


def test_series_files_are_written_for_charting(screened):
    data, out = screened
    names = {p.stem for p in (out / "series").glob("*.json")}
    assert names == {"TIGHT", "WIDE"}
    series = json.loads((out / "series" / "TIGHT.json").read_text())
    assert len(series["c"]) == len(series["d"]) == len(series["ma50"])
    assert series["ma200"][-1] is not None


def test_meta_is_written(screened):
    _, out = screened
    meta = json.loads((out / "meta.json").read_text())
    assert meta["counts"]["ready"] == 1
    assert meta["as_of"]


def test_a_stale_symbol_is_dropped(tmp_path):
    """A delisted stock keeps its history; it must not be screened as current."""
    import pandas as pd
    from screener.config import load_config

    cfg = load_config()
    df = build_base(t3_low=55.3)
    df["volume"] *= 40
    bench = benchmark_like(df["close"])

    fresh = evaluate_symbol("X", "", "", df, bench, cfg, market_date=df.index[-1])
    assert fresh is not None and fresh["bucket"] != "rejected"

    # Same data, but the market has moved on by a month.
    stale = evaluate_symbol(
        "X", "", "", df, bench, cfg,
        market_date=df.index[-1] + pd.Timedelta(days=30),
    )
    assert stale is None
