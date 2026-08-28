import pandas as pd

from screener import stage2
from screener.config import load_config
from tests.synth import _bars, benchmark_like, leg, stage2_vcp

CFG = load_config()


def _checks(result):
    return {c.key: c.passed for c in result.checks}


def test_stage2_template_passes_on_an_advance():
    df = stage2_vcp()
    res = stage2.evaluate(df, CFG, benchmark_like(df["close"]))
    checks = _checks(res)
    for key in (
        "ma150_above_ma200",
        "ma150_rising",
        "ma200_rising",
        "price_above_ma150_ma200",
        "price_above_ma50",
        "ma_stack",
        "more_up_weeks",
    ):
        assert checks[key], f"{key} should pass on a Stage 2 advance"


def test_stage2_rejects_a_downtrend():
    closes = leg(100, 40, 380) + [40.0]
    df = _bars(closes, [3_000_000] * len(closes), seed=7)
    res = stage2.evaluate(df, CFG, benchmark_like(df["close"]))
    checks = _checks(res)
    assert not res.passed
    assert not checks["ma150_above_ma200"]
    assert not checks["ma150_rising"]
    assert not checks["ma_stack"]


def test_beta_check_fails_without_benchmark():
    df = stage2_vcp()
    res = stage2.evaluate(df, CFG, None)
    assert not _checks(res)["beta"]


def test_market_cap_check_only_appears_when_configured():
    df = stage2_vcp()
    cfg = load_config()
    assert "market_cap" not in _checks(stage2.evaluate(df, cfg, None))
    cfg["stage2"]["min_market_cap"] = 2_000_000_000
    res = stage2.evaluate(df, cfg, None, market_cap=5_000_000_000)
    assert _checks(res)["market_cap"]
