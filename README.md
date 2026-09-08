# Stage 2 + VCP Stock Screener

A free, phone-first screener for US stocks that applies Mark Minervini's Stage 2
trend template and then looks for a Volatility Contraction Pattern, reporting the
pivot, the stop-loss line, the risk percentage and the reward-to-risk ratio for
every setup it finds.

It runs as a scheduled GitHub Action and publishes a static, installable web app
to GitHub Pages. There is nothing to host and no API key to buy.

---

## First, an honest note about "every second, 24/7"

The screen is built on **daily** bars: 50/150/200-day moving averages, weekly
candles, and consolidations measured in weeks. None of those can change more
than once a day. The US market is also only open 9:30–16:00 ET on weekdays, so
there is no 24/7 data to react to.

Re-scanning ~6,000 tickers every second is also not something any free data
source will allow — you would be rate-limited within minutes.

So this runs **once per trading day, after the close**, and gives you the
complete picture the next morning: which stocks are in Stage 2, which have a
valid VCP, where the pivot is, and where the stop goes. The only thing that
genuinely moves intraday is whether price has crossed the pivot — and that is a
single number per stock that your broker or TradingView alert already watches
for free.

If you later want live intraday tracking of the shortlist, the cleanest free
route is your own free API key (Finnhub, Alpaca) held in the browser's local
storage, polling only the 10–40 names on the list. Ask and it can be added.

---

## Setup (about five minutes, once)

1. **Merge this branch into your default branch.** GitHub only runs `schedule:`
   workflows from the default branch — a nightly scan on a feature branch will
   never fire.
2. **Settings → Pages → Source: GitHub Actions.** The workflow deploys the app
   itself; you do not need a `gh-pages` branch.
3. **Actions → Nightly scan → Run workflow.** Start with `limit = 300` for a
   quick first run that proves the pipeline end to end (a couple of minutes).
   Then run it again with `limit = 0` for the full universe. The first full run
   backfills two years of history for every symbol and takes roughly 30–60
   minutes; later runs reuse the cache and take a few minutes.
4. **Open the Pages URL on your phone** (Actions run summary → `github-pages`
   environment) and add it to your home screen: Safari → Share → *Add to Home
   Screen*, or Chrome → ⋮ → *Add to Home screen*. It then behaves like an app,
   works offline from the last scan, and remembers your starred names.

Keep the repository **public** — public repos get unlimited GitHub Actions
minutes, so the whole thing stays free.

### Why this stays running

GitHub disables scheduled workflows after **60 days of repository inactivity**,
and Pages here is deployed from an artifact rather than a commit — so the
nightly runs would otherwise leave no trace in the repo and the schedule would
switch itself off after about two months, silently. Each run therefore records
its result to `state/last-scan.json` and commits it. That both keeps the cron
alive and gives you a history of when the scan last ran and what it found.

If you ever see the app's date going stale, check Actions first: GitHub emails
the repo owner before disabling a schedule, and re-enabling is one click.

---

## What the screen actually checks

### Stage 2 trend template

| # | Rule | Where it lives |
|---|------|----------------|
| 1 | 150MA is above the 200MA | `stage2.ma_mid`, `stage2.ma_long` |
| 2 | Both the 150MA and 200MA slope upward | `stage2.slope_lookback_days` (default: higher than 20 sessions ago) |
| 3 | Price is above both the 150MA and 200MA | — |
| 4 | Price > 50MA > 150MA > 200MA, stacked in order | `stage2.ma_short` |
| 5 | More up weeks than down weeks | `stage2.weekly_lookback_weeks` (52 weeks; a green week closes above its own open) |

### The TradingView filters, reproduced

| Filter | Setting |
|--------|---------|
| 1. Price > SMA50 | included in the stack check above |
| 2. SMA50 > SMA150 | included in the stack check above |
| 3. SMA150 > SMA200 | rule 1 above |
| 4. Market cap floor | `stage2.min_market_cap` — **currently 0 (off)**, per your choice to let dollar volume do the filtering |
| 5. Monthly volume × price > $900M | `stage2.liquidity.min_dollar_volume`, summed over 21 sessions |
| 6. Beta ≥ 1 over 1 year | `stage2.beta` — computed from 252 daily returns against SPY |

Every check is reported individually, so each stock's detail page shows exactly
which rules it passes and by how much.

### VCP

The brief defines a consolidation by how it *ends*: price drops to A and bounces;
if it later breaks below A, then A was only a midpoint; when it drops to B and
never goes below B again, that consolidation is complete and B is the stop-loss
reference.

The detector implements that rule directly, using nested extremes:

```
H1 = highest high in the base window
L1 = lowest low after H1      -> nothing after L1 is lower, by construction
H2 = highest high after L1    -> H2 <= H1, by construction
L2 = lowest low after H2      -> L2 >= L1, by construction
...
```

Each `(Hn, Ln)` pair is one complete T cycle. Because each low is the minimum of
everything that follows its high, no low in the sequence is ever undercut later —
which is exactly the condition the brief describes. The same construction forces
descending highs and ascending lows, i.e. the wedge shape of a VCP.

What the screen then verifies is that the pattern is genuinely *contracting*:

- each T is at most 85% as deep as the one before it (`vcp.contraction_shrink_factor`)
- average volume falls from each T to the next (`vcp.require_volume_contraction`)
- the final T is tight — within `vcp.final_depth_min_pct`–`final_depth_max_pct`
- volume has dried up: the last 5 sessions average under 85% of the 50-day
  average (`vcp.dryup_ratio`)
- the final low has held for at least a few sessions (`vcp.min_bars_since_final_low`)
- price is still near the pivot rather than extended away from it

### The trade levels

- **Pivot** = the high of the final contraction. This is the resistance line to
  break, and the buy trigger is a break above it on surging volume
  (`vcp.breakout_volume_multiple`, default 1.5× the 50-day average).
- **Support / stop** = the low of the final contraction — the low the pattern
  says should not be broken.
- **Risk** = `(pivot − support) / pivot`, capped at **5%** (`risk.max_risk_pct`).
- **Target** = the measured move: `pivot + (base high − base low)`.
- **Reward:risk** = `(target − pivot) / (pivot − support)`, minimum 3
  (`risk.min_reward_risk`).

> **A note on 4–8% versus 5% risk.** Because risk is defined as pivot-to-support,
> and the pivot and support are the two ends of the final contraction, the risk
> percentage *is* the final contraction's depth. A 5% risk ceiling therefore
> only admits final contractions of 5% or tighter — the bottom half of the 4–8%
> range in the brief. Setups whose shape is right but whose final T is 5–12% are
> not discarded: they land in the **Watch** tab with the reason shown. Raise
> `risk.max_risk_pct` to 8 if you would rather see them in **Ready**.

---

## The three tabs

- **Ready** — passes every Stage 2 rule, has a valid VCP, risk ≤ 5%, reward:risk
  ≥ 3, and price is in the buy zone. On most days this list is short or empty.
  That is the screen working, not a bug.
- **Watch** — Stage 2 plus a real contraction structure, but one hard rule is not
  met yet (usually risk above 5%). The card states which.
- **Stage 2** — passes the trend template but has no valid base yet. This is your
  pool of candidates to keep an eye on.

Star any name to pin it to the ★ tab; stars are stored on your phone.

Each detail page carries the chart (candles, 50/150/200MAs, shaded T cycles,
pivot and stop lines), the trade plan, a position-size calculator, the
contraction table, and the full pass/fail checklist for both engines.

---

## Tuning

Everything is in [`config.yaml`](config.yaml); no code changes are needed. The
knobs you are most likely to want:

```yaml
risk:
  max_risk_pct: 5.0        # raise to 8 to match the 4-8% final-T range
  min_reward_risk: 3.0

stage2:
  min_market_cap: 0        # set 2_000_000_000 to restore the $2B floor
  liquidity:
    min_dollar_volume: 900000000

vcp:
  min_contractions: 2      # 3 is stricter and closer to textbook T1/T2/T3
  final_depth_max_pct: 12.0
  base_lookback_days: 130  # how far back a base may start
```

Market cap is currently off. If you turn it on, note that neither free data
source provides market cap, so a source for it would need to be wired in first —
open an issue and it can be added.

---

## Running it locally

```bash
pip install -r requirements.txt
python -m screener.run --limit 200 -v          # quick scan
python -m screener.run --symbols AAPL,NVDA,MSFT
python -m pytest                                # 28 tests, no network needed
python -m http.server 8000 --directory docs     # then open localhost:8000
```

`--no-fetch` reuses the local `cache/prices.sqlite` without touching the network.

## Layout

```
screener/
  universe.py    US common-stock list (NASDAQ Trader, SEC fallback)
  fetch.py       Stooq -> Yahoo daily bars, SQLite cache
  indicators.py  SMAs, slopes, weekly candles, beta, dollar volume
  stage2.py      the trend template and the TradingView filters
  vcp.py         contraction detection, pivot, support, risk, reward
  score.py       ranking
  run.py         orchestration and JSON output
docs/            the phone app (published to GitHub Pages)
tests/           synthetic-pattern tests for every engine
```

## Data sources

Stooq for daily bars with Yahoo Finance as a per-symbol fallback; the NASDAQ
Trader symbol directory for the universe, with the SEC ticker file as backup.
All free, none requiring a key. If one source rate-limits mid-run the fetcher
disables it and continues on the other.

---

*This is a screening tool, not investment advice. It reports what the rules say;
the decisions are yours.*
