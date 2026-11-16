| Dashboard on a private repo? | GitHub Pages needs a **public** repo on the free plan, and the site is public even on Pro. | Public repo, positions withheld from the page |
| Quiver Quantitative? |# Market Radar

A market monitoring engine that watches instruments you choose, evaluates rules
you wrote, and pushes an alert to your phone when one fires — naming the
instrument you can actually buy from an Irish Revolut account.

It runs on GitHub Actions for free, has **zero third-party dependencies**, and
publishes a dashboard you can install on your iPhone home screen.

```
┌────────────────┐    ┌──────────────┐    ┌───────────────┐    ┌──────────┐
│ Twelve Data    │    │ GitHub       │    │ Pushover      │    │ You open │
│ Kraken         │──▶ │ Actions      │──▶ │ push to your  │──▶ │ Revolut  │
│ Coinbase       │    │ (cron, free) │    │ phone         │    │ and act  │
└────────────────┘    └──────┬───────┘    └───────────────┘    └──────────┘
                             │
                             ▼
                      GitHub Pages
                   installable dashboard
```

## What this is not

- **Not financial advice.** Every alert is a rule you configured firing on past
  price data. It is a tripwire, not a recommendation, and not a forecast.
- **Not an auto-trader.** It places no orders anywhere. Revolut offers no
  retail trading API, so you execute every trade yourself, deliberately. That
  is a feature: an alert you have to act on is an alert you have to think about.
- **Not intraday.** GitHub's scheduler runs on a best-effort basis and can be
  late. Daily bars, days-to-weeks horizon. Do not build scalping on this.

## Why it is built this way

Every design decision below came from testing the alternatives, not from
assuming. The tests are reproducible — they are in the git history of this
README.

| Question | What I found | Decision |
|---|---|---|
| Trade on Revolut automatically? | Revolut Trading supports no algorithmic trading and publishes no retail API. Only Revolut X (crypto) has one. | Alert → you execute manually |
| Which ETFs can an Irish account buy? | EEA retail **cannot** buy US-domiciled ETFs (SPY, QQQ, VOO) — no PRIIPs KID. Only EU-registered UCITS ETFs. | Watchlist maps proxy → UCITS ISIN |
| Yahoo Finance for data? | Returned **HTTP 429** from a residential Mac and needs a cookie+crumb handshake; worse from CI IPs. | Fallback only, never depended on |
| Stooq for free EU data? | Now behind a JavaScript proof-of-work bot wall. Returns JS, not CSV. | Unusable, dropped |
| Twelve Data free tier? | Works. 800 req/day, 8/min, US equities + ETFs + forex + crypto. European venues need the $79/mo Grow plan. | Primary equity source |
| Free EU/UK equity data? | Does not meaningfully exist. Alpha Vantage gives ~25 req/day; Marketstack 100/month. | Measure US-listed proxies instead |
| Crypto data? | Kraken and Coinbase are public, keyless, and quote in EUR. Both verified working. | Primary crypto source, costs nothing |
| Alerts to an iPhone? | Pushover uses native APNs, has 5 priority levels, and can repeat until acknowledged. $4.99 once. | Primary channel (Telegram supported too) |
| Quiver Quantitative? | Real Claude connector, good alternative data, but needs a $30–75/mo API plan. | Wired in docs, switched off |

### The proxy mapping, which is the heart of it

Two constraints collide: free data covers US symbols and not EU venues, and an
Irish Revolut account can buy EU UCITS ETFs and not US ones. So every watchlist
entry carries two symbols:

```toml
[[instrument]]
signal_symbol = "SPY"               # where the data comes from (free, liquid)
trade_ticker  = "SXR8"              # what you actually buy
trade_isin    = "IE00B5BMR087"      # how to find it in the Revolut app
trade_venue   = "Xetra"
```

Both track the S&P 500, so trend and momentum signals carry across. Every alert
states both, so you never get told to buy something you cannot buy.

What does *not* carry across is currency: a EUR share class of a USD index also
moves with EUR/USD. That is why `eurusd` is on the watchlist.

## Quick start

Needs Python 3.11+ (for the built-in TOML reader). Nothing else — no pip
install, no virtualenv, no requirements file.

```bash
python3 -m radar selftest      # 15 offline checks, no network or keys needed
python3 -m radar check         # validate your config
python3 -m radar snapshot      # current readings as a table (needs a data key)
python3 -m radar run --dry-run # full cycle, no alerts sent
```

To deploy it as an always-on service:

```bash
bash deploy.sh
```

That creates the GitHub repo, uploads everything as one commit, and turns on
the dashboard — over the GitHub API with `curl`, so no `git` is needed. Full
walkthrough in **[SETUP.md](SETUP.md)**, about 15 minutes including signups.

## Commands

| Command | What it does |
|---|---|
| `radar selftest` | End-to-end test on synthetic data. No network, no keys. Run this first. |
| `radar check` | Validates config, lists proxy mappings, reports missing keys. |
| `radar run` | Fetch, evaluate, alert, publish the dashboard. |
| `radar run --dry-run` | Everything except sending real alerts. |
| `radar run --force` | Ignore cooldowns — re-send everything currently true. |
| `radar snapshot` | Current readings for the whole watchlist as a table. |
| `radar test-alert` | Prove your Pushover keys work. |
| `radar size <id>` | Position size implied by your risk settings. |

## Configuration

Four files in `config/`, all commented:

- **`radar.toml`** — timezone, alert channels, severity floor, cooldown, quiet
  hours, risk budget.
- **`watchlist.toml`** — what to watch, and the Revolut instrument for each.
- **`rules.toml`** — the tripwires and their severities.
- **`portfolio.toml`** — your open positions, with the stops and targets *you*
  chose. Leave it empty to run as a pure watchlist.

### The dial that matters most

`notify.min_level` in `radar.toml`. Start at `low`, live with it for a week,
then raise it. An alerting system you mute is worse than none.

Severity maps to behaviour:

| Level | Pushover behaviour |
|---|---|
| `info` | Dashboard only — never pushed |
| `low` / `medium` | Normal push |
| `high` | Bypasses your phone's silent mode |
| `critical` | Repeats every 5 min for an hour until you acknowledge it |

Only `critical` ignores quiet hours, and only breached stops are `critical` by
default.

### Rule kinds

| Kind | Fires when | Key params |
|---|---|---|
| `ma_cross` | Fast MA crosses slow MA | `fast`, `slow`, `direction`, `use_ema` |
| `rsi` | RSI crosses a threshold | `period`, `below`, `above` |
| `breakout` | Close clears the prior N-day high/low | `period`, `direction` |
| `pullback_to_ma` | Price touches a rising MA | `period`, `tolerance_pct`, `trend_period` |
| `drawdown_from_high` | N% below the lookback high | `lookback`, `pct` |
| `volume_spike` | Volume z-score above a threshold | `period`, `z` |
| `pct_move` | Moved N% over N days | `days`, `pct`, `direction` |
| `macd_cross` | MACD crosses its signal line | `fast`, `slow`, `signal`, `direction` |
| `bollinger_break` | Close outside a Bollinger band | `period`, `mult`, `direction` |
| `vol_regime` | Realised vol crosses a threshold | `period`, `above_pct` |
| `near_extreme` | Within N% of a 52-week extreme | `lookback`, `within_pct`, `side` |

Threshold rules fire on the **crossing bar only**, not every day the condition
stays true. Combined with the cooldown, that is what keeps the volume sane.

## How the noise is controlled

A signal must clear four gates to reach your phone:

1. Severity at or above `min_level`.
2. Not already sent within `cooldown_hours` — **unless it has escalated**, so a
   "watch this" that becomes "stop breached" always gets through.
3. Not inside quiet hours, unless its level is in `always_send_levels`.
4. Inside the per-run budget, highest severity first — so a market-wide selloff
   cannot produce forty pushes.

Everything suppressed still appears on the dashboard. Nothing is silently lost.

## Running costs

| Item | Cost |
|---|---|
| GitHub Actions | €0 (free for public repos; ~4 min/day on private) |
| GitHub Pages | €0 |
| Twelve Data | €0 (free tier; ~50 of 800 daily requests used) |
| Kraken / Coinbase | €0 (keyless public endpoints) |
| Pushover | €4.99 once |
| **Total** | **€4.99, one time** |

Optional: Alpha Vantage (free, ~25 req/day) for EU-listed spot checks; Quiver
Quantitative from $30/mo for congressional, insider and 13F data.

## Honest limitations

- **Proxy tracking error.** SPY and SXR8 track the same index but are not the
  same instrument: different currency, domicile, fees, and trading hours. A
  signal on the proxy is a signal about the index, not a quote for your order.
- **Scheduler lateness.** GitHub cron can run 5–30 minutes late under load.
- **IEX-style partial data.** Free feeds are thinner than paid ones. Daily
  closes are reliable; sub-daily precision is not.
- **These rules are not edges.** Moving-average crossovers and RSI thresholds
  are among the most widely known indicators in existence. Treat them as
  attention-direction, not as alpha.
- **Survivorship and backtest absence.** Nothing here is backtested. A rule
  firing does not mean it has ever been profitable.
- **Crypto fees.** Revolut charges up to 1.49% on crypto versus ~0.25% on
  equities. Short-horizon crypto ideas face a high hurdle before they break even.

## Architecture

```
radar/
  indicators.py   SMA, EMA, RSI (Wilder), ATR, MACD, Bollinger, Donchian, z-score
  signals.py      rule evaluators, dispatched on Rule.kind; memoised per instrument
  portfolio.py    stop/target/trailing checks, position sizing, concentration
  providers/      Twelve Data, Kraken, Coinbase, Yahoo, Alpha Vantage + fallback chain
  notify/         Pushover, Telegram, console + the four alert gates
  state.py        SQLite: bar cache, alert cooldowns, signal history
  dashboard.py    writes docs/data.json
  selftest.py     15 offline checks — the thing to run after any change
docs/             installable PWA dashboard (static, dependency-free)
.github/workflows radar.yml (scheduled engine), test.yml (CI on push)
```

The bar cache is not only a speed optimisation: if every provider is rate-limited,
a run still produces signals from the last good data, and says so in the
dashboard's diagnostics rather than pretending the market was quiet.

## Licence

Private, personal use. Quiver, Twelve Data and Alpha Vantage free tiers all
prohibit commercial redistribution of their data — if you ever share this,
check their terms first.
