# Setup

Two accounts to create, one command to run, three secrets to paste. About 15
minutes, most of it waiting on signups.

Get the keys first — the deploy script is the quick part.

---

## 1. Twelve Data — market data (free, 2 min)

1. Sign up for the **Basic** plan at **https://twelvedata.com/pricing**.
   Free, no card.
2. Copy your API key.

Covers US stocks, US ETFs, forex and crypto — which, thanks to the proxy
mapping, covers the whole default watchlist. Crypto also works with no key at
all via Kraken.

## 2. Pushover — phone alerts (€4.99 once, 5 min)

1. Install **Pushover** from the App Store, create an account.
2. On **https://pushover.net**, copy your **User Key** from the front page.
3. Scroll to *Your Applications* → **Create an Application**, call it
   `Market Radar`, and copy the **API Token** it gives you.

Free for 30 days, then €4.99 once, forever. Chosen because it uses native APNs
and can repeat an emergency alert until you acknowledge it — which is what you
want when a stop is breached.

> Prefer free? Message `@BotFather` on Telegram, send `/newbot`, copy the
> token, message your new bot, then open
> `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your chat id. Set
> `channels = ["telegram", "console"]` in `config/radar.toml`.

## 3. Deploy (one command, 3 min)

You need a GitHub account (**https://github.com/signup**) and a token.

**Create the token:** [github.com/settings/tokens/new](https://github.com/settings/tokens/new?scopes=repo,workflow&description=Market%20Radar%20deploy)
— choose **Tokens (classic)**, and make sure both **`repo`** and **`workflow`**
are ticked. The `workflow` scope is not optional: without it GitHub refuses to
accept the scheduled-run file and the push fails.

Then, in Terminal:

```bash
cd ~/Documents/market-radar && bash deploy.sh
```

It creates the repository, uploads all 43 files as one clean commit, turns on
the dashboard, and prints what is left. The token is read hidden, never written
to disk, and cleared from memory when it finishes.

This uses the GitHub API over `curl`, not `git` — deliberately, because this
Mac has no working git.

### Public or private?

The script asks. The honest trade-off:

|  | Public | Private |
|---|---|---|
| Phone dashboard | ✅ Works | ❌ Free plan can't serve Pages from a private repo |
| Actions minutes | Unlimited | 2,000/month (this uses ~120) |
| Who sees your watchlist | Anyone | Only you |
| Who sees your positions | **Nobody either way** — see below | Only you |

**Public is the recommended default**, because your positions stay private
regardless: they go in an encrypted secret (step 4), and the engine withholds
all position sizes, entry prices and P/L from the published page. The dashboard
shows your watchlist and signals; the sensitive numbers only ever reach your
phone.

Note that even on GitHub Pro, a Pages *site* is public — only Enterprise can
make it private. So this split is the right design on any plan.

## 4. Paste your secrets (3 min)

The script prints the direct link. It's:

`https://github.com/<you>/market-radar/settings/secrets/actions`

**New repository secret**, three times:

| Name | Value |
|---|---|
| `TWELVEDATA_API_KEY` | your Twelve Data key |
| `PUSHOVER_TOKEN` | the Pushover **application** token |
| `PUSHOVER_USER` | your Pushover **user** key |

Optional but recommended once you hold anything:

| Name | Value |
|---|---|
| `RADAR_PORTFOLIO_TOML` | the contents of `config/portfolio.toml`, with your real positions |

That last one is how positions stay out of a public repo. If the secret is set,
the engine uses it and ignores the file — so you can leave the committed
`portfolio.toml` empty.

Optional extras: `ALPHAVANTAGE_API_KEY` (spot-check real EU-listed prices),
`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`.

## 5. First run (2 min)

`Actions` → **Market Radar** → **Run workflow** → tick **dry_run** → Run.

It runs 16 offline self-checks, validates your config, fetches real prices,
evaluates every rule, publishes the dashboard — and sends nothing.

Read the log:

- **Self-test fails?** Stop. The failure names the property that broke.
- **Data fetch fails?** The log names the provider and the reason.
- **All green?** Run it again without `dry_run`. If anything is currently
  true, your phone buzzes.

Nothing fired? That's the normal state — rules are tripwires, not a feed. To
force one, use **Run workflow → force**, which re-sends everything currently
true, ignoring cooldowns.

## 6. Put it on your phone (1 min)

Open `https://<you>.github.io/market-radar/` in **Safari** on your iPhone —
it must be Safari; Chrome on iOS cannot install web apps.

**Share** → **Add to Home Screen** → **Add**.

Own icon, no browser chrome, works offline from cache, follows your light/dark
setting. Give Pages a minute or two to go live after the first run.

## 7. Make it yours

This is the part that actually matters. The defaults are a starting point, not
a portfolio.

**`config/watchlist.toml`** — delete what you do not care about. Twenty
instruments you understand beats a hundred you do not. For anything you add,
fill in `trade_isin` so alerts tell you what to search for in Revolut.

**`config/radar.toml`** — after a week, raise `min_level` from `low` to
`medium`. Set `account_size` to switch on position sizing and concentration
warnings.

**Your positions** — put them in the `RADAR_PORTFOLIO_TOML` secret, with the
stop and target *you* decided on. This is where the engine earns its keep: a
`critical` alert the moment your own stop is breached, repeating until you
acknowledge it.

Edit files directly on github.com — click the file, then the pencil icon. Every
commit runs the test workflow, so a typo is caught immediately instead of
silently breaking the next scheduled run.

---

## When it runs

| Cron (UTC) | Why |
|---|---|
| 07:30 Mon–Fri | Before the European open |
| 12:00 Mon–Fri | Midday Europe |
| 21:45 Mon–Fri | After the US close, so the daily bar is final |
| 10:00 Sat–Sun | Crypto only — it never closes |

Change them at the top of `.github/workflows/radar.yml`. GitHub enforces a
5-minute minimum and can run late under load.

## Optional: run it locally too

This Mac currently has **no working Python** — macOS ships a stub that only
prompts for Xcode, and even once installed it is Python 3.9, which is too old
(`tomllib` arrived in 3.11). To run the engine locally, install a current
Python from **https://www.python.org/downloads/macos/**, then:

```bash
cd ~/Documents/market-radar && python3 -m radar selftest
```

The cloud deployment needs none of this.

---

## Troubleshooting

**`deploy.sh` says the token lacks the workflow scope** — exactly what it
sounds like. Create a new classic token with both `repo` and `workflow` ticked
and re-run; the script is safe to run twice.

**"Twelve Data rejected 'SYMBOL'"** — either the symbol is not on the free tier
(European venues are not) or you are out of daily credits. The free plan allows
800/day; the default watchlist uses about 50.

**"Yahoo would not issue a crumb"** — expected and harmless. Yahoo rate-limits
CI IPs hard. It is only a fallback, so the run continues.

**No alerts but the dashboard updates** — most likely working as designed.
Check the **History** tab: items marked *dashboard only* were suppressed by
`min_level`, a cooldown, or quiet hours.

**Dashboard says "awaiting first run"** — Pages is serving the placeholder
`docs/data.json`. Run the workflow once and give Pages a minute.

**Positions tab says figures are withheld** — by design on a public page. Set
`include_positions = true` in `config/radar.toml` only if the repo and site are
genuinely private to you.

**Scheduled runs stop after a couple of months** — GitHub silently disables
cron on public repos after 60 days of no activity. The daily dashboard commit
normally counts, but if it happens, push any commit and re-enable the workflow.
