"""Nightly scan: universe -> prices -> Stage 2 -> VCP -> JSON for the web app.

Run with `python -m screener.run`. Output lands in docs/data/, which GitHub
Pages serves directly to the phone.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import adjust as adjmod
from . import indicators as ind
from . import stage2 as s2mod
from . import vcp as vcpmod
from .config import ROOT, load_config
from .fetch import PriceStore, update_prices
from .score import score
from .universe import build_universe

log = logging.getLogger("screener")


def _round(x, nd=2):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if not math.isfinite(v) else round(v, nd)


def _series_payload(df: pd.DataFrame, bars: int, cfg: dict) -> dict:
    """Compact OHLCV + moving averages for the phone chart."""
    s2 = cfg.get("stage2", {})
    close = df["close"]
    mas = {
        f"ma{n}": ind.sma(close, int(n))
        for n in (s2.get("ma_short", 50), s2.get("ma_mid", 150), s2.get("ma_long", 200))
    }
    tail = df.tail(bars)
    payload = {
        "d": [d.strftime("%Y-%m-%d") for d in tail.index],
        "o": [_round(x, 4) for x in tail["open"]],
        "h": [_round(x, 4) for x in tail["high"]],
        "l": [_round(x, 4) for x in tail["low"]],
        "c": [_round(x, 4) for x in tail["close"]],
        "v": [int(x) if math.isfinite(x) else 0 for x in tail["volume"]],
    }
    for name, series in mas.items():
        payload[name] = [_round(x, 4) for x in series.tail(bars)]
    return payload


def evaluate_symbol(symbol: str, name: str, exchange: str, df: pd.DataFrame,
                    bench_close: pd.Series, cfg: dict,
                    market_date: pd.Timestamp | None = None) -> dict | None:
    """Run both engines against one symbol and return a result row."""
    ucfg = cfg.get("universe", {})
    if len(df) < int(ucfg.get("min_history_days", 260)):
        return None

    # A delisted or halted symbol keeps its history in the cache. Without this
    # its stale close would be screened as though it were today's price.
    max_stale = int(ucfg.get("max_staleness_days", 7) or 0)
    if market_date is not None and max_stale > 0:
        if (market_date - df.index[-1]).days > max_stale:
            return None
    price = float(df["close"].iloc[-1])
    if not math.isfinite(price) or price < float(ucfg.get("min_price", 0)):
        return None

    st = s2mod.evaluate(df, cfg, bench_close)
    st.metrics["rs_126d"] = _round(ind.relative_strength(df["close"], bench_close, 126))
    st.metrics["rs_63d"] = _round(ind.relative_strength(df["close"], bench_close, 63))

    prev = float(df["close"].iloc[-2]) if len(df) > 1 else float("nan")
    row = {
        "symbol": symbol,
        "name": name,
        "exchange": exchange,
        "price": _round(price, 4),
        "prev_close": _round(prev, 4),
        "change_pct": _round(ind.pct_from(price, prev)),
        "date": df.index[-1].strftime("%Y-%m-%d"),
        "stage2": st.as_dict(),
    }

    if not st.passed:
        row["bucket"] = "rejected"
        return row

    vres = vcpmod.detect(df, cfg)
    row["vcp"] = vres.as_dict()

    if vres.passed:
        row["bucket"] = "ready"
    elif vres.contractions and vres.status in ("actionable", "broke_out"):
        # Shape is there but a hard rule (usually the 5% risk ceiling) is not
        # met yet. Worth watching rather than discarding.
        row["bucket"] = "watch"
    else:
        row["bucket"] = "stage2"

    row["score"] = score(vres.metrics, st.metrics)
    return row


def _thin_row(row: dict) -> dict:
    """Trim a Stage 2-only row down to what the list view needs."""
    m = row["stage2"]["metrics"]
    return {
        "symbol": row["symbol"],
        "name": row["name"],
        "exchange": row["exchange"],
        "price": row["price"],
        "change_pct": row.get("change_pct"),
        "date": row["date"],
        "bucket": row["bucket"],
        "score": row.get("score", 0),
        "metrics": {
            k: m.get(k)
            for k in (
                "ma50", "ma150", "ma200", "ma150_slope_pct", "ma200_slope_pct",
                "up_weeks", "down_weeks", "beta", "dollar_volume",
                "rs_126d", "pct_off_52w_high",
            )
        },
        "vcp_reason": (row.get("vcp") or {}).get("reason", ""),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 2 + VCP stock screener")
    parser.add_argument("--config", default=None)
    parser.add_argument("--cache", default=str(ROOT / "cache" / "prices.sqlite"))
    parser.add_argument("--out", default=str(ROOT / "docs" / "data"))
    parser.add_argument("--limit", type=int, default=0, help="cap the universe size")
    parser.add_argument("--symbols", default="", help="comma-separated symbols to scan instead")
    parser.add_argument("--no-fetch", action="store_true", help="use only cached bars")
    parser.add_argument("--force-full", action="store_true", help="refetch full history")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    started = time.time()
    cfg = load_config(args.config)
    if args.limit:
        cfg.setdefault("universe", {})["limit"] = args.limit

    out_dir = Path(args.out)
    series_dir = out_dir / "series"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. universe -------------------------------------------------------
    if args.symbols:
        universe = [
            {"symbol": s.strip().upper(), "name": "", "exchange": ""}
            for s in args.symbols.split(",")
            if s.strip()
        ]
    else:
        universe = build_universe(cfg, ROOT / "cache" / "universe.json")
    log.info("universe: %d symbols", len(universe))

    benchmark = str(cfg.get("data", {}).get("benchmark", "SPY"))
    store = PriceStore(args.cache)

    # 2. prices ---------------------------------------------------------
    symbols = [r["symbol"] for r in universe]
    if not args.no_fetch:
        stats = update_prices(
            sorted({*symbols, benchmark}), store, cfg, force_full=args.force_full
        )
        log.info("fetch: %s", stats)
        store.prune(int(cfg.get("data", {}).get("history_days", 800)) + 60)

    adjust_splits = bool(cfg.get("data", {}).get("adjust_splits", False))
    bench_df = store.load(benchmark)
    if adjust_splits:
        bench_df, bench_splits = adjmod.back_adjust(bench_df)
        if bench_splits:
            log.info("benchmark %s back-adjusted for %d split(s)", benchmark, len(bench_splits))
    if bench_df.empty:
        log.error("no benchmark data for %s - beta and RS cannot be computed", benchmark)
        bench_close = pd.Series(dtype=float)
    else:
        bench_close = bench_df["close"]

    # 3. evaluate -------------------------------------------------------
    history_days = int(cfg.get("data", {}).get("history_days", 800))
    # The benchmark trades every session, so its last bar is the market's
    # newest session and the reference for judging a symbol stale.
    market_date = bench_df.index[-1] if not bench_df.empty else None
    rows: list[dict] = []
    errors = 0
    adjusted_symbols = 0
    for i, meta in enumerate(universe, start=1):
        sym = meta["symbol"]
        try:
            df = store.load(sym, history_days)
            if df.empty:
                continue
            # Detection only by default: see data.adjust_splits in config.yaml
            # for why inferring splits from price and volume is not safe.
            if adjust_splits:
                df, splits = adjmod.back_adjust(df)
            else:
                splits = adjmod.detect_splits(df)
            if splits:
                adjusted_symbols += 1
                log.debug("%s: back-adjusted %d split(s): %s", sym, len(splits),
                          [(a.date, a.factor) for a in splits])
            row = evaluate_symbol(sym, meta.get("name", ""), meta.get("exchange", ""),
                                  df, bench_close, cfg, market_date)
            if row and row["bucket"] != "rejected":
                if splits:
                    # Reported so a suspect series is visible, not silently
                    # rewritten.
                    row["price_steps"] = [
                        {"date": a.date, "factor": a.factor} for a in splits
                    ]
                rows.append(row)
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the scan
            errors += 1
            log.debug("%s failed: %s", sym, exc)
        if i % 500 == 0:
            log.info("evaluated %d/%d (kept %d)", i, len(universe), len(rows))

    order = {"ready": 0, "watch": 1, "stage2": 2}
    rows.sort(key=lambda r: (order.get(r["bucket"], 9), -r.get("score", 0)))

    ready = [r for r in rows if r["bucket"] == "ready"]
    watch = [r for r in rows if r["bucket"] == "watch"]
    stage_only = [r for r in rows if r["bucket"] == "stage2"]

    ocfg = cfg.get("output", {})
    stage_only = stage_only[: int(ocfg.get("max_stage2_only", 600))]

    # 4. write ----------------------------------------------------------
    if series_dir.exists():
        shutil.rmtree(series_dir)
    series_dir.mkdir(parents=True, exist_ok=True)
    max_series = int(ocfg.get("max_series", 400))
    series_bars = int(ocfg.get("series_bars", 260))
    written = 0
    for row in (ready + watch)[:max_series]:
        df = store.load(row["symbol"], history_days)
        if df.empty:
            continue
        payload_series = _series_payload(df, series_bars, cfg)
        (series_dir / f"{row['symbol']}.json").write_text(
            json.dumps(payload_series, separators=(",", ":"))
        )
        # Contraction indices are positions in the full history; the chart only
        # receives the tail, so rebase them onto the series that ships with it.
        offset = len(df) - len(payload_series["c"])
        for c in row.get("vcp", {}).get("contractions", []):
            c["high_idx"] -= offset
            c["low_idx"] -= offset
        row["has_series"] = True
        written += 1

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": max((r["date"] for r in rows), default=None),
        "universe_size": len(universe),
        "counts": {
            "ready": len(ready),
            "watch": len(watch),
            "stage2": len(stage_only),
            "errors": errors,
            "price_steps_flagged": adjusted_symbols,
        },
        "config": {
            "max_risk_pct": cfg.get_path("risk.max_risk_pct"),
            "min_reward_risk": cfg.get_path("risk.min_reward_risk"),
            "min_contractions": cfg.get_path("vcp.min_contractions"),
            "final_depth_pct": [
                cfg.get_path("vcp.final_depth_min_pct"),
                cfg.get_path("vcp.final_depth_max_pct"),
            ],
            "dryup_ratio": cfg.get_path("vcp.dryup_ratio"),
            "min_dollar_volume": cfg.get_path("stage2.liquidity.min_dollar_volume"),
            "min_beta": cfg.get_path("stage2.beta.min_beta"),
            "min_market_cap": cfg.get_path("stage2.min_market_cap"),
        },
        "ready": ready,
        "watch": watch,
        "stage2": [_thin_row(r) for r in stage_only],
    }
    (out_dir / "screen.json").write_text(json.dumps(payload, separators=(",", ":")))
    (out_dir / "meta.json").write_text(
        json.dumps(
            {
                "generated_at": payload["generated_at"],
                "as_of": payload["as_of"],
                "counts": payload["counts"],
                "elapsed_sec": round(time.time() - started, 1),
            },
            indent=2,
        )
    )
    store.close()

    log.info(
        "done in %.1fs - ready=%d watch=%d stage2=%d series=%d errors=%d",
        time.time() - started, len(ready), len(watch), len(stage_only), written, errors,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
