# Market Radar — notes for Claude

## What this is

A rule-based market monitoring engine. It watches instruments, evaluates rules
from `config/`, and pushes alerts. It does **not** place trades and must not be
made to: Revolut offers no retail trading API, and execution stays manual by
design.

## Hard boundaries

- No personalised investment advice, price targets, or buy/sell/hold calls.
- Signals are descriptive: "the 50-day crossed the 200-day", never "buy this".
- The disclaimer in `radar/notify/__init__.py` goes on every alert. Keep it.

## Architecture

Zero third-party dependencies, by choice — it runs unattended in CI and must
not break because a package published a bad release. Pure stdlib only:
`urllib`, `tomllib`, `sqlite3`, `json`. Do not add `requests`, `pandas` or
`numpy`; `radar/indicators.py` implements what is needed.

```
radar/
  indicators.py   pure-Python technical indicators
  signals.py      rule evaluators, dispatched on Rule.kind
  portfolio.py    stop/target/trailing checks, position sizing
  providers/      data sources behind one interface, with fallback
  notify/         pushover / telegram / console, plus the alert gates
  state.py        sqlite: bar cache, alert cooldowns, signal history
  dashboard.py    writes docs/data.json for the PWA
  selftest.py     offline end-to-end test — run this after any change
```

## Data source constraints (researched, not assumed)

- **Twelve Data free**: US equities, ETFs, forex, crypto. 800 req/day, 8/min.
  European venues need the $79/mo plan.
- **Yahoo**: global coverage but rate-limits hard (verified HTTP 429 from both
  a residential IP and CI). Fallback only — never depend on it.
- **Stooq**: now behind a JavaScript proof-of-work wall. Unusable from scripts.
- **Kraken / Coinbase**: public, keyless, EUR-quoted. Reliable.
- **Alpha Vantage free**: ~25 req/day. Enough to spot-check a couple of
  EU-listed lines, not a watchlist.

This is why watchlist entries carry both `signal_symbol` (where data comes
from) and `trade_ticker`/`trade_isin` (what an Irish Revolut account can buy).
EEA retail cannot buy US-domiciled ETFs at all — no PRIIPs KID. Preserve that
split in any change.

## Adding things

- **A rule kind**: write `eval_*` in `signals.py`, register it in
  `EVALUATORS`, then extend `check_all_rule_kinds_run` in `selftest.py` —
  that check asserts every kind is exercised and will fail if you forget.
- **A provider**: subclass `Provider`, return bars oldest-first, raise
  `ProviderError` with `retryable` set correctly, add it to `build_providers`
  and the right `CHAINS` entry.
- **A notifier**: match the `Notifier` protocol and register it in
  `build_notifiers`.

## Before committing

```bash
python3 -m radar selftest && python3 -m radar check && python3 -m unittest discover -s tests
```

## Position privacy (load-bearing)

A GitHub Pages site is public on every plan below Enterprise. So:

- Holdings load from `RADAR_PORTFOLIO_TOML` (an encrypted repo secret) in
  preference to `config/portfolio.toml`, so a public repo need not contain
  them — see `load_portfolio` in `radar/config.py`.
- `dashboard.include_positions` defaults to **false**, and `build_payload`
  emits `{"withheld": true, ...}` instead of position rows. Stop and target
  alerts still go to the phone; they are simply not published.

`check_portfolio_privacy` in `selftest.py` asserts no position figure reaches
the payload. Do not weaken that default to make the dashboard look fuller.
