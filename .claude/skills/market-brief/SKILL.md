---
name: market-brief
description: Research the context behind Market Radar signals. Use when the user asks why a signal fired, what the news is on a watchlist instrument, or for a written market brief on what the radar is currently flagging. Reads docs/data.json for current state.
---

# Market brief

The radar answers "what moved". This skill answers "why, and what else is true" —
the qualitative layer that price rules cannot see.

## Boundaries

State these if the user asks for a recommendation:

- You are not a licensed financial adviser and must not give personalised
  investment advice, price targets, or buy/sell/hold recommendations.
- You research, summarise and lay out considerations. The user decides.
- Revolut has no retail trading API. Every trade is placed by the user, by
  hand, in the app. Never attempt to place one.

What you *can* do freely: gather facts, summarise news and filings, explain
what an indicator measures, lay out the bull and bear case side by side,
identify what would have to be true for a thesis to work, and point out risks
the price data does not show.

## Workflow

1. Read `docs/data.json` for the current state — `signals_now`, `snapshots`,
   `positions`, `problems`.
2. For each instrument worth covering, research: recent news, upcoming or just
   -passed earnings, sector context, and anything that explains the move.
3. If the Quiver Quantitative connector is available, add the alternative-data
   view: congressional trades, insider (Form 4) activity, 13F changes,
   government contracts, lobbying. Say explicitly when it is not available
   rather than guessing.
4. Write the brief.

## Brief format

```
## <date> — what the radar is flagging

### Needs a decision
<critical/high signals: what fired, what the context is, what the user's own
configured levels say, and what the realistic options are — including "do
nothing", which is usually the right default>

### Worth knowing
<medium/low signals, grouped by theme rather than listed one by one>

### Context the rules cannot see
<earnings dates, news, macro events, anything structural>

### Open questions
<what you could not determine, and what would resolve it>
```

## Rules for the writing

- Lead with what changed since the last brief, not with a market recap.
- Always name the Revolut-tradeable instrument, not just the proxy symbol.
  `snapshots[].trade_label` has it. The user cannot buy SPY from Ireland.
- Quantify. "Down 12% from its July high, RSI 28" beats "weak".
- Distinguish fact from interpretation in the sentence itself.
- Flag when a signal is probably noise. A 5% move on an earnings day is
  expected, not informative.
- Note the currency effect on EUR-denominated share classes of USD indices
  when EUR/USD has moved materially.
- If `problems` is non-empty, say what data was missing. A signal that did not
  fire because data was absent is not the same as a quiet market.
