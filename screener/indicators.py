"""Price-series indicators used by the Stage 2 template.

Everything here takes a tidy OHLCV DataFrame indexed by date (ascending) with
float columns: open, high, low, close, volume.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

OHLCV = ("open", "high", "low", "close", "volume")


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average, matching TradingView's SMA."""
    return series.rolling(window=window, min_periods=window).mean()


def slope_pct(series: pd.Series, lookback: int) -> float:
    """Percent change of a series over `lookback` bars, using the last value.

    Used for "is the 150MA pointing up and to the right?". Returns NaN when
    there is not enough history.
    """
    if len(series) <= lookback:
        return float("nan")
    now = series.iloc[-1]
    then = series.iloc[-1 - lookback]
    if not np.isfinite(now) or not np.isfinite(then) or then == 0:
        return float("nan")
    return float((now - then) / abs(then) * 100.0)


def to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Resample daily bars into weekly candles (week ending Friday).

    Partial trailing weeks are kept - a week in progress is still a candle on
    the weekly chart.
    """
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    weekly = df.resample("W-FRI").agg(agg)
    return weekly.dropna(subset=["open", "close"])


def up_down_weeks(df: pd.DataFrame, lookback_weeks: int) -> tuple[int, int]:
    """Count green vs red weekly candles over the last `lookback_weeks`.

    A green week is one that closes above its own open, i.e. a green candle on
    the weekly chart.
    """
    weekly = to_weekly(df).tail(lookback_weeks)
    if weekly.empty:
        return 0, 0
    up = int((weekly["close"] > weekly["open"]).sum())
    down = int((weekly["close"] < weekly["open"]).sum())
    return up, down


def beta(stock_close: pd.Series, bench_close: pd.Series, lookback: int) -> float:
    """Beta of the stock against the benchmark over `lookback` trading days.

    Computed from daily simple returns on the dates the two series share, which
    is what TradingView's 1-year beta measures.
    """
    joined = pd.concat(
        {"s": stock_close, "b": bench_close}, axis=1, join="inner"
    ).dropna()
    rets = joined.pct_change().dropna().tail(lookback)
    if len(rets) < max(30, lookback // 4):
        return float("nan")
    var_b = float(rets["b"].var(ddof=1))
    if var_b <= 0:
        return float("nan")
    cov = float(rets["s"].cov(rets["b"]))
    return cov / var_b


def dollar_volume(df: pd.DataFrame, days: int, aggregate: str = "sum") -> float:
    """Volume x price over the last `days` sessions (TradingView filter 5)."""
    tail = df.tail(days)
    if tail.empty:
        return float("nan")
    dv = (tail["close"] * tail["volume"]).astype(float)
    return float(dv.sum() if aggregate == "sum" else dv.mean())


def avg_volume(df: pd.DataFrame, days: int) -> float:
    tail = df["volume"].tail(days)
    if len(tail) < 1:
        return float("nan")
    return float(tail.mean())


def pct_from(value: float, reference: float) -> float:
    """Percent distance of `value` from `reference`."""
    if not np.isfinite(value) or not np.isfinite(reference) or reference == 0:
        return float("nan")
    return float((value - reference) / reference * 100.0)


def relative_strength(
    stock_close: pd.Series, bench_close: pd.Series, days: int
) -> float:
    """Stock's return minus the benchmark's return over `days` sessions."""
    joined = pd.concat(
        {"s": stock_close, "b": bench_close}, axis=1, join="inner"
    ).dropna()
    if len(joined) <= days:
        return float("nan")
    window = joined.tail(days + 1)
    s = float(window["s"].iloc[-1] / window["s"].iloc[0] - 1.0) * 100
    b = float(window["b"].iloc[-1] / window["b"].iloc[0] - 1.0) * 100
    return s - b


def week52(df: pd.DataFrame, days: int = 252) -> tuple[float, float]:
    tail = df.tail(days)
    return float(tail["low"].min()), float(tail["high"].max())


def rs_score(close: pd.Series) -> float:
    """Weighted 12-month price performance, the input to the RS Rating.

    The classic form weights the most recent quarter double, so a stock that
    has just started outperforming ranks above one coasting on gains made a
    year ago:  2*(P/P-63) + (P/P-126) + (P/P-189) + (P/P-252).

    This is a raw score, meaningless on its own - it only becomes an RS Rating
    once ranked against every other stock in the universe.
    """
    c = close.dropna().to_numpy(dtype=float)
    if len(c) < 253:
        return float("nan")
    last = c[-1]
    if not np.isfinite(last) or last <= 0:
        return float("nan")
    legs = []
    for lag, weight in ((63, 2.0), (126, 1.0), (189, 1.0), (252, 1.0)):
        past = c[-1 - lag]
        if not np.isfinite(past) or past <= 0:
            return float("nan")
        legs.append(weight * (last / past))
    return float(sum(legs))


def rs_ratings(scores: dict[str, float]) -> dict[str, int]:
    """Percentile-rank raw RS scores into IBD-style 1-99 ratings.

    99 means the stock outperformed 99% of the market. The rank is taken over
    every symbol that has a score, not only the ones that survive the screen -
    "beats 90% of all stocks" has to mean all stocks.
    """
    usable = {s: v for s, v in scores.items() if np.isfinite(v)}
    if not usable:
        return {}
    order = sorted(usable, key=lambda s: usable[s])
    n = len(order)
    out: dict[str, int] = {}
    for i, sym in enumerate(order):
        # Fraction of the field this symbol beats, mapped onto 1-99.
        pct = i / (n - 1) if n > 1 else 1.0
        out[sym] = int(min(99, max(1, round(pct * 98) + 1)))
    return out
