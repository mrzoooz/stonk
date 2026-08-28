"""Stage 2 trend template.

Implements the five Stage 2 criteria from the brief together with the six
TradingView filters that reproduce the manual screen. Each check is reported
individually so the app can show a per-stock checklist rather than a bare
pass/fail.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

import numpy as np
import pandas as pd

from . import indicators as ind


@dataclass
class Check:
    key: str
    label: str
    passed: bool
    detail: str = ""


@dataclass
class Stage2Result:
    passed: bool
    checks: list[Check] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "checks": [asdict(c) for c in self.checks],
            "metrics": self.metrics,
        }

    @property
    def failed_keys(self) -> list[str]:
        return [c.key for c in self.checks if not c.passed]


def _f(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return float("nan")
    return v


def _ok(x: float) -> bool:
    return bool(np.isfinite(x))


def evaluate(
    df: pd.DataFrame,
    cfg: dict,
    bench_close: pd.Series | None = None,
    market_cap: float | None = None,
) -> Stage2Result:
    """Run the Stage 2 template against one symbol's daily bars."""
    s2 = cfg.get("stage2", {})
    n_short = int(s2.get("ma_short", 50))
    n_mid = int(s2.get("ma_mid", 150))
    n_long = int(s2.get("ma_long", 200))

    close = df["close"]
    ma_s = ind.sma(close, n_short)
    ma_m = ind.sma(close, n_mid)
    ma_l = ind.sma(close, n_long)

    price = _f(close.iloc[-1])
    v_s, v_m, v_l = _f(ma_s.iloc[-1]), _f(ma_m.iloc[-1]), _f(ma_l.iloc[-1])

    lookback = int(s2.get("slope_lookback_days", 20))
    min_slope = float(s2.get("min_slope_pct", 0.0))
    slope_m = ind.slope_pct(ma_m, lookback)
    slope_l = ind.slope_pct(ma_l, lookback)

    checks: list[Check] = []

    # Criterion 1 - the 150MA sits above the 200MA.
    checks.append(
        Check(
            "ma150_above_ma200",
            f"{n_mid}MA above {n_long}MA",
            _ok(v_m) and _ok(v_l) and v_m > v_l,
            f"{v_m:.2f} vs {v_l:.2f}" if _ok(v_m) and _ok(v_l) else "insufficient history",
        )
    )

    # Criterion 2 - both the 150MA and the 200MA slope upward.
    checks.append(
        Check(
            "ma150_rising",
            f"{n_mid}MA rising",
            _ok(slope_m) and slope_m > min_slope,
            f"{slope_m:+.2f}% over {lookback}d" if _ok(slope_m) else "insufficient history",
        )
    )
    checks.append(
        Check(
            "ma200_rising",
            f"{n_long}MA rising",
            _ok(slope_l) and slope_l > min_slope,
            f"{slope_l:+.2f}% over {lookback}d" if _ok(slope_l) else "insufficient history",
        )
    )

    # Criterion 3 - price above both the 150MA and the 200MA.
    checks.append(
        Check(
            "price_above_ma150_ma200",
            f"Price above {n_mid}MA and {n_long}MA",
            _ok(price) and _ok(v_m) and _ok(v_l) and price > v_m and price > v_l,
            f"{price:.2f} vs {v_m:.2f} / {v_l:.2f}"
            if _ok(v_m) and _ok(v_l)
            else "insufficient history",
        )
    )

    # TradingView filter 1 - price above the 50MA.
    checks.append(
        Check(
            "price_above_ma50",
            f"Price above {n_short}MA",
            _ok(price) and _ok(v_s) and price > v_s,
            f"{price:.2f} vs {v_s:.2f}" if _ok(v_s) else "insufficient history",
        )
    )

    # Criterion 4 / filters 2-3 - the full stack, in order.
    stacked = all(_ok(x) for x in (price, v_s, v_m, v_l)) and price > v_s > v_m > v_l
    checks.append(
        Check(
            "ma_stack",
            f"Price > {n_short}MA > {n_mid}MA > {n_long}MA",
            stacked,
            f"{price:.2f} > {v_s:.2f} > {v_m:.2f} > {v_l:.2f}"
            if all(_ok(x) for x in (price, v_s, v_m, v_l))
            else "insufficient history",
        )
    )

    # Criterion 5 - more up weeks than down weeks.
    weeks = int(s2.get("weekly_lookback_weeks", 52))
    up_w, down_w = ind.up_down_weeks(df, weeks)
    checks.append(
        Check(
            "more_up_weeks",
            "More up weeks than down weeks",
            (up_w > down_w) if s2.get("require_more_up_weeks", True) else True,
            f"{up_w} up / {down_w} down over {weeks}w",
        )
    )

    # TradingView filter 5 - monthly volume x price.
    liq = s2.get("liquidity", {})
    dv_days = int(liq.get("dollar_volume_days", 21))
    dv = ind.dollar_volume(df, dv_days, str(liq.get("aggregate", "sum")))
    min_dv = float(liq.get("min_dollar_volume", 0))
    checks.append(
        Check(
            "dollar_volume",
            f"Volume x price over {dv_days}d above ${min_dv/1e6:,.0f}M",
            _ok(dv) and dv >= min_dv,
            f"${dv/1e6:,.0f}M" if _ok(dv) else "no data",
        )
    )

    # TradingView filter 6 - beta over one year.
    bcfg = s2.get("beta", {})
    b = (
        ind.beta(close, bench_close, int(bcfg.get("lookback_days", 252)))
        if bench_close is not None
        else float("nan")
    )
    min_beta = float(bcfg.get("min_beta", 1.0))
    checks.append(
        Check(
            "beta",
            f"Beta at least {min_beta:g} (1y)",
            _ok(b) and b >= min_beta,
            f"{b:.2f}" if _ok(b) else "no benchmark data",
        )
    )

    # TradingView filter 4 - market cap floor, skipped when set to 0.
    min_cap = float(s2.get("min_market_cap", 0) or 0)
    if min_cap > 0:
        cap = _f(market_cap)
        checks.append(
            Check(
                "market_cap",
                f"Market cap above ${min_cap/1e9:,.1f}B",
                _ok(cap) and cap >= min_cap,
                f"${cap/1e9:,.2f}B" if _ok(cap) else "unknown",
            )
        )

    metrics = {
        "price": round(price, 4) if _ok(price) else None,
        "ma50": round(v_s, 4) if _ok(v_s) else None,
        "ma150": round(v_m, 4) if _ok(v_m) else None,
        "ma200": round(v_l, 4) if _ok(v_l) else None,
        "ma150_slope_pct": round(slope_m, 3) if _ok(slope_m) else None,
        "ma200_slope_pct": round(slope_l, 3) if _ok(slope_l) else None,
        "up_weeks": up_w,
        "down_weeks": down_w,
        "dollar_volume": round(dv, 0) if _ok(dv) else None,
        "beta": round(b, 3) if _ok(b) else None,
        "market_cap": market_cap,
    }

    lo52, hi52 = ind.week52(df)
    metrics["low_52w"] = round(lo52, 4)
    metrics["high_52w"] = round(hi52, 4)
    metrics["pct_off_52w_high"] = round(ind.pct_from(price, hi52), 2)
    metrics["pct_above_52w_low"] = round(ind.pct_from(price, lo52), 2)

    return Stage2Result(all(c.passed for c in checks), checks, metrics)
