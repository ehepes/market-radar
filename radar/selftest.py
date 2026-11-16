"""Offline end-to-end self-test.

Runs the whole engine against synthetic price data with known properties: no
network, no API keys, no config files required. This is the first thing to run
after cloning, and it is what CI runs before every live cycle.

Each check states the property it is proving, so a failure tells you what
broke rather than just that something did.
"""

from __future__ import annotations

import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from . import indicators as ind
from .config import (
    Config,
    DashboardSettings,
    EngineSettings,
    NotifySettings,
    RiskSettings,
    load_config,
)
from .models import Bar, Holding, Instrument, ProviderError, Rule
from .notify import format_signal, in_quiet_hours, select_for_sending
from .notify.console import ConsoleNotifier
from .portfolio import evaluate_holdings, position_size
from .providers import MarketData
from .providers.base import Provider, make_bar
from .signals import evaluate, snapshot
from .state import Store

START = date(2025, 1, 1)


# ------------------------------------------------------------------ fixtures


def bars_from(closes: Sequence[float], volumes: Optional[Sequence[float]] = None) -> List[Bar]:
    """Synthetic daily bars with a plausible 2% intraday range."""
    out: List[Bar] = []
    for i, close in enumerate(closes):
        volume = volumes[i] if volumes else 1_000_000.0
        out.append(
            Bar(
                day=START + timedelta(days=i),
                open=close * 0.995,
                high=close * 1.01,
                low=close * 0.99,
                close=float(close),
                volume=float(volume),
            )
        )
    return out


def instrument(**kwargs) -> Instrument:
    defaults = dict(
        id="test",
        name="Test Instrument",
        asset_class="equity",
        signal_symbol="TEST",
        rules=[],
    )
    defaults.update(kwargs)
    return Instrument(**defaults)  # type: ignore[arg-type]


def make_config(**overrides) -> Config:
    config = Config(
        engine=EngineSettings(),
        notify=NotifySettings(channels=["console"], cooldown_hours=48, min_level="low"),
        risk=RiskSettings(account_size=10_000.0, risk_per_trade_pct=1.0, max_position_pct=20.0),
        dashboard=DashboardSettings(include_positions=True),
        instruments=[instrument()],
        rules={},
        holdings=[],
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


# -------------------------------------------------------------------- checks


def check_indicators() -> None:
    """Indicators produce known values and warm up at the right index."""
    assert ind.sma([1, 2, 3, 4, 5], 5)[-1] == 3.0, "SMA of 1..5 should be 3"
    assert ind.sma([1, 2, 3], 5) == [None, None, None], "SMA must stay None until warm"

    ema = ind.ema([1, 2, 3, 4, 5], 3)
    assert ema[1] is None and ema[2] == 2.0, "EMA seeds with the SMA at index period-1"

    rising = list(range(1, 40))
    assert ind.last(ind.rsi(rising, 14)) == 100.0, "RSI of a monotonic rise is 100"
    flat = [100.0] * 30
    assert ind.last(ind.rsi(flat, 14)) == 50.0, "RSI of a flat series is defined as 50"

    bars = bars_from([100, 102, 101, 103, 105, 104, 106, 108, 107, 109] * 3)
    atr = ind.last(
        ind.atr([b.high for b in bars], [b.low for b in bars], [b.close for b in bars], 14)
    )
    assert atr is not None and atr > 0, "ATR should be positive on moving data"

    hi, lo = ind.donchian([b.high for b in bars], [b.low for b in bars], 5)
    assert hi[4] is None, "Donchian needs `period` prior bars before it is defined"
    assert hi[5] == max(b.high for b in bars[0:5]), "Donchian must exclude the current bar"

    assert ind.crossed_up([1.0, 3.0], [2.0, 2.0]), "crossed_up should detect a fresh cross"
    assert not ind.crossed_up([3.0, 4.0], [2.0, 2.0]), "already-above is not a cross"
    assert not ind.crossed_up([None, 4.0], [2.0, 2.0]), "a None history is not a cross"

    vol = ind.last(ind.realized_vol([100 * (1.01**i) for i in range(40)], 20))
    assert vol is not None and vol >= 0, "realised vol should be defined and non-negative"


def check_ma_cross_fires_on_the_cross() -> None:
    """A golden cross fires on the crossing bar and not afterwards."""
    rule = Rule(id="x", kind="ma_cross", level="high", params={"fast": 3, "slow": 5, "direction": "up"})
    inst = instrument(rules=["x"])

    # SMA3 overtakes SMA5 exactly on the final bar of this series.
    crossing = [10, 9, 8, 7, 6, 5, 6, 8, 10]
    signals, problems = evaluate(inst, bars_from(crossing), [rule])
    assert not problems, f"unexpected problems: {problems}"
    assert len(signals) == 1, f"expected 1 cross signal, got {len(signals)}"
    assert signals[0].level == "high", "signal must inherit the rule's level"
    assert "ABOVE" in signals[0].headline, f"unexpected headline: {signals[0].headline}"

    # One bar later the cross is history, so it must not re-fire.
    later, _ = evaluate(inst, bars_from(crossing + [12]), [rule])
    assert not later, "a cross must not re-fire on subsequent bars"


def check_rsi_fires_exactly_once() -> None:
    """Crossing a threshold alerts once, not every day it stays breached."""
    rule = Rule(id="r", kind="rsi", level="medium", params={"period": 14, "below": 30})
    inst = instrument(rules=["r"])

    closes = [100.0] * 20
    price = 100.0
    for _ in range(15):
        price *= 0.97
        closes.append(price)

    fires = 0
    for end in range(16, len(closes) + 1):
        signals, _ = evaluate(inst, bars_from(closes[:end]), [rule])
        fires += len(signals)
    assert fires == 1, f"RSI threshold should fire exactly once, fired {fires} times"


def check_breakout_and_volume() -> None:
    """Breakout compares against prior bars; volume spike is a z-score."""
    base = [95 + (i % 10) for i in range(30)]
    breakout_rule = Rule(id="b", kind="breakout", params={"period": 20, "direction": "up"})
    inst = instrument(rules=["b"])

    quiet, _ = evaluate(inst, bars_from(base), [breakout_rule])
    assert not quiet, "a range-bound series should not report a breakout"

    signals, _ = evaluate(inst, bars_from(base + [130]), [breakout_rule])
    assert len(signals) == 1, "a decisive new high should report a breakout"

    volumes = [1_000_000.0] * 20 + [5_000_000.0]
    vol_rule = Rule(id="v", kind="volume_spike", params={"period": 20, "z": 2.5})
    signals, _ = evaluate(
        instrument(rules=["v"]), bars_from(base[:21], volumes), [vol_rule]
    )
    assert len(signals) == 1, "a 5x volume day should clear a 2.5 sigma threshold"


def check_all_rule_kinds_run() -> None:
    """Every documented rule kind evaluates without raising."""
    from .signals import EVALUATORS

    closes = [100 + 20 * (i / 300) + (5 if i % 7 == 0 else 0) for i in range(300)]
    bars = bars_from(closes)
    rules = [
        Rule(id="ma", kind="ma_cross", params={"fast": 50, "slow": 200}),
        Rule(id="rsi", kind="rsi", params={"period": 14, "below": 30, "above": 70}),
        Rule(id="bo", kind="breakout", params={"period": 20}),
        Rule(id="pb", kind="pullback_to_ma", params={"period": 50, "trend_period": 200}),
        Rule(id="dd", kind="drawdown_from_high", params={"lookback": 252, "pct": 10}),
        Rule(id="vs", kind="volume_spike", params={"period": 20, "z": 2.5}),
        Rule(id="pm", kind="pct_move", params={"days": 5, "pct": 5}),
        Rule(id="mac", kind="macd_cross", params={}),
        Rule(id="bb", kind="bollinger_break", params={"period": 20}),
        Rule(id="vr", kind="vol_regime", params={"period": 20, "above_pct": 40}),
        Rule(id="ne", kind="near_extreme", params={"lookback": 252, "within_pct": 2}),
    ]
    assert {r.kind for r in rules} == set(EVALUATORS), (
        "selftest must exercise every rule kind; missing: "
        f"{set(EVALUATORS) - {r.kind for r in rules}}"
    )

    signals, problems = evaluate(instrument(rules=[r.id for r in rules]), bars, rules)
    assert not problems, f"rule evaluation raised: {problems}"
    assert isinstance(signals, list)

    bad = Rule(id="bad", kind="does_not_exist", params={})
    _, problems = evaluate(instrument(rules=["bad"]), bars, [bad])
    assert problems and "unknown kind" in problems[0], "unknown rule kinds must be reported"


def check_snapshot() -> None:
    """Snapshots expose the fields the dashboard renders."""
    snap = snapshot(instrument(), bars_from([100 + i * 0.3 for i in range(300)]))
    for field in ("close", "sma50", "sma200", "rsi14", "change_1d", "from_252d_high"):
        assert field in snap, f"snapshot missing '{field}'"
    assert snap["sma50"] is not None and snap["sma200"] is not None
    assert snapshot(instrument(), [])["status"] == "no data"


def check_portfolio() -> None:
    """Stops, targets and trailing stops fire off your own configured levels."""
    inst = instrument(id="eq", trade_ticker="XYZ", trade_venue="Xetra")
    config = make_config(
        instruments=[inst],
        holdings=[
            Holding(instrument_id="eq", quantity=10, entry_price=100, stop=95, target=120)
        ],
    )

    stopped, rows = evaluate_holdings(config, {"eq": bars_from([100, 98, 94])})
    assert any(s.rule_id == "stop_breached" for s in stopped), "a breached stop must alert"
    assert all(s.level == "critical" for s in stopped if s.rule_id == "stop_breached")
    assert rows and rows[0]["pnl_pct"] < 0, "P/L should be negative below entry"

    hit, _ = evaluate_holdings(config, {"eq": bars_from([100, 110, 125])})
    assert any(s.rule_id == "target_reached" for s in hit), "a reached target must alert"

    quiet, _ = evaluate_holdings(config, {"eq": bars_from([100, 101, 102])})
    assert not quiet, "a position between its levels should stay silent"

    trailing_config = make_config(
        instruments=[inst],
        holdings=[
            Holding(instrument_id="eq", quantity=10, entry_price=100, trailing_stop_pct=10)
        ],
    )
    trailed, _ = evaluate_holdings(trailing_config, {"eq": bars_from([100, 150, 130])})
    assert any(s.rule_id == "trailing_stop_breached" for s in trailed), (
        "130 is more than 10% below the 150 peak, so the trailing stop must fire"
    )


def check_position_sizing() -> None:
    """Sizing is arithmetic on your risk budget, and respects your cap."""
    risk = RiskSettings(account_size=10_000, risk_per_trade_pct=1.0, max_position_pct=20.0)

    sized = position_size(risk, price=100.0, stop=95.0)
    # Risk 1% of 10k = 100 EUR; 5 EUR of risk per unit -> 20 units.
    assert sized["units"] == 20.0, f"expected 20 units, got {sized['units']}"
    assert sized["notional"] == 2000.0

    # A tight stop would imply a huge position, so the cap must bind.
    capped = position_size(risk, price=100.0, stop=99.5)
    assert capped["capped_by_max_position"] is True, "max_position_pct must cap the size"
    assert capped["notional"] == 2000.0, "cap is 20% of a 10k account"

    # A wider cap, so the ATR arithmetic is what is being tested here and not
    # the concentration limit: 2,500 notional would otherwise be capped to 2,000.
    roomy = RiskSettings(account_size=10_000, risk_per_trade_pct=1.0, max_position_pct=50.0)
    atr_based = position_size(roomy, price=100.0, atr=2.0, atr_multiple=2.0)
    assert atr_based["units"] == 25.0, "1% of 10k over a 4.00 ATR stop is 25 units"
    assert atr_based["capped_by_max_position"] is False

    assert "error" in position_size(RiskSettings(account_size=0), price=100, stop=95)
    assert "error" in position_size(risk, price=100.0, stop=105.0), (
        "a stop above the price is invalid for a long"
    )


def check_cooldown_and_gates() -> None:
    """Cooldowns suppress repeats; escalation and budgets behave."""
    from .notify import dispatch

    inst = instrument(id="eq")
    config = make_config(instruments=[inst])
    rule = Rule(id="x", kind="ma_cross", level="medium", params={"fast": 3, "slow": 5})
    signals, _ = evaluate(
        instrument(id="eq", rules=["x"]), bars_from([10, 9, 8, 7, 6, 5, 6, 8, 10]), [rule]
    )
    assert signals, "fixture should produce a signal"

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")

        first = dispatch(config, store, signals, [ConsoleNotifier()])
        assert first["sent"] == 1, "a fresh signal should send"

        second = dispatch(config, store, signals, [ConsoleNotifier()])
        assert second["sent"] == 0, "the same signal must be suppressed by the cooldown"
        assert second["suppressed"] == 1

        escalated = [
            type(signals[0])(
                instrument_id="eq",
                rule_id="x",
                level="critical",
                headline="now critical",
                detail="escalated",
            )
        ]
        third = dispatch(config, store, escalated, [ConsoleNotifier()])
        assert third["sent"] == 1, "an escalation must break through the cooldown"

        # Severity floor
        config.notify.min_level = "high"
        low = [
            type(signals[0])(
                instrument_id="eq", rule_id="q", level="low", headline="h", detail="d"
            )
        ]
        assert select_for_sending(config, store, low)[0] == [], (
            "signals below min_level must be filtered out"
        )

        # Per-run budget, highest severity kept
        config.notify.min_level = "low"
        config.notify.max_alerts_per_run = 1
        many = [
            type(signals[0])(
                instrument_id="eq", rule_id=f"r{i}", level=level, headline="h", detail="d"
            )
            for i, level in enumerate(["low", "critical", "medium"])
        ]
        to_send, suppressed, _ = select_for_sending(config, store, many)
        assert len(to_send) == 1 and to_send[0].level == "critical", (
            "the budget must keep the most severe signal"
        )
        assert len(suppressed) == 2
        store.close()


def check_quiet_hours() -> None:
    """Quiet windows work across midnight."""
    config = make_config()
    config.notify.quiet_start = "23:00"
    config.notify.quiet_end = "07:00"
    assert in_quiet_hours(config, datetime(2026, 1, 1, 23, 30)), "23:30 is inside 23:00-07:00"
    assert in_quiet_hours(config, datetime(2026, 1, 1, 3, 0)), "03:00 is inside 23:00-07:00"
    assert not in_quiet_hours(config, datetime(2026, 1, 1, 12, 0)), "midday is outside"

    config.notify.quiet_start = "09:00"
    config.notify.quiet_end = "17:00"
    assert in_quiet_hours(config, datetime(2026, 1, 1, 10, 0))
    assert not in_quiet_hours(config, datetime(2026, 1, 1, 20, 0))

    config.notify.quiet_start = None
    assert not in_quiet_hours(config), "no window configured means never quiet"


def check_alert_formatting() -> None:
    """Alerts name the instrument you can actually buy, and say what they are."""
    from .notify import DISCLAIMER

    inst = instrument(
        id="sp500",
        name="S&P 500",
        signal_symbol="SPY",
        trade_ticker="SXR8",
        trade_venue="Xetra",
        trade_isin="IE00B5BMR087",
        trade_note="UCITS equivalent; US-listed SPY is unavailable to EEA retail.",
    )
    assert inst.is_proxy, "a differing trade ticker makes this a proxy mapping"

    rule = Rule(id="x", kind="ma_cross", level="high", params={"fast": 3, "slow": 5})
    signals, _ = evaluate(inst, bars_from([10, 9, 8, 7, 6, 5, 6, 8, 10]), [rule])
    title, body = format_signal(signals[0], inst)

    assert "S&P 500" in title
    assert "SPY" in body, "the body must state what was measured"
    assert "SXR8" in body and "IE00B5BMR087" in body, "the body must state what to buy"
    assert DISCLAIMER in body, "every alert carries the not-advice line"

    plain = instrument(id="p", name="Plain", signal_symbol="AAPL", trade_ticker="AAPL")
    assert not plain.is_proxy, "identical tickers are not a proxy"


def check_provider_fallback() -> None:
    """A failing provider falls through; a total outage falls back to cache."""

    class Boom(Provider):
        name = "twelvedata"

        def daily_bars(self, symbol: str, days: int) -> List[Bar]:
            raise ProviderError("simulated rate limit", retryable=True)

    class Works(Provider):
        name = "yahoo"

        def daily_bars(self, symbol: str, days: int) -> List[Bar]:
            return bars_from([100, 101, 102])

    inst = instrument(id="eq", signal_provider="auto")
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        market = MarketData(cache=store, providers={"twelvedata": Boom(), "yahoo": Works()})
        bars = market.bars(inst, 100)
        assert len(bars) == 3, "should fall through to the working provider"
        assert market.sources["eq"] == "yahoo"
        assert market.errors["eq"], "the failure should still be reported"

        dead = MarketData(cache=store, providers={"twelvedata": Boom()})
        cached = dead.bars(inst, 100)
        assert len(cached) == 3, "a total outage must fall back to cached bars"
        assert dead.sources["eq"] == "cache"
        store.close()


def check_bar_parsing() -> None:
    """Provider rows with nulls and odd dates are handled, not guessed at."""
    assert make_bar("2026-01-05", 1, 2, 0.5, 1.5, 100) is not None
    assert make_bar("2026-01-05", None, None, None, None, None) is None, (
        "a bar with no close is unusable"
    )
    assert make_bar("2026-01-05", None, None, None, 1.5, None).open == 1.5, (
        "missing OHLC fields fall back to the close"
    )
    assert make_bar("2026-01-05", 1, 2, 0.5, -3, 100) is None, "a negative close is invalid"
    assert make_bar(1767225600, 1, 2, 0.5, 1.5, 100) is not None, "epoch seconds parse"


def check_shipped_config() -> None:
    """The config files in this repo load and validate."""
    config_dir = Path("config")
    if not (config_dir / "watchlist.toml").exists():
        print("    (skipped: run from the repo root to validate shipped config)")
        return

    config = load_config(config_dir)
    assert config.instruments, "watchlist.toml should define instruments"
    assert config.rules, "rules.toml should define rules"
    assert not config.unknown_rule_refs()

    from .signals import EVALUATORS

    unknown = {r.kind for r in config.rules.values()} - set(EVALUATORS)
    assert not unknown, f"shipped rules.toml uses unknown kinds: {unknown}"

    for inst in config.instruments:
        assert inst.rules, f"instrument '{inst.id}' has no rules attached"
        if inst.asset_class in ("equity", "etf") and inst.is_proxy:
            assert inst.trade_note, (
                f"proxy instrument '{inst.id}' should explain why it is a proxy"
            )


def check_dashboard_payload() -> None:
    """The dashboard payload is JSON-serialisable and complete."""
    import json

    from . import dashboard

    inst = instrument(id="eq", name="Eq")
    config = make_config(instruments=[inst])
    bars = bars_from([100 + i * 0.2 for i in range(260)])
    rule = Rule(id="bo", kind="breakout", params={"period": 20})
    signals, _ = evaluate(inst, bars, [rule])

    payload = dashboard.build_payload(
        config=config,
        snapshots=[snapshot(inst, bars)],
        signals=signals,
        positions=[],
        recent_signals=[],
        problems=[],
        sources={"eq": "twelvedata"},
        sparklines=dashboard.sparkline_data({"eq": bars}),
    )
    text = json.dumps(payload)
    assert '"generated_at"' in text and '"snapshots"' in text
    assert payload["counts"]["instruments"] == 1
    spark = payload["sparklines"]["eq"]
    assert len(spark) == 60 and 0 <= min(spark) and max(spark) <= 100, (
        "sparklines are normalised to 0-100"
    )


def check_portfolio_privacy() -> None:
    """Positions load from a secret, and stay off a public dashboard."""
    import json
    import os

    from . import dashboard
    from .config import DashboardSettings, load_portfolio

    # Holdings can come from an env var, so a public repo need not contain them.
    os.environ["RADAR_PORTFOLIO_TOML"] = (
        "[[holding]]\ninstrument_id = 'eq'\nquantity = 5\nentry_price = 20\nstop = 18\n"
    )
    try:
        holdings = load_portfolio(Path("does-not-exist.toml"))
        assert len(holdings) == 1, "RADAR_PORTFOLIO_TOML must override the file"
        assert holdings[0].stop == 18
    finally:
        del os.environ["RADAR_PORTFOLIO_TOML"]

    inst = instrument(id="eq", name="Eq")
    rows = [{"instrument_id": "eq", "value": 1234.5, "cost": 1000.0, "entry_price": 20.0}]

    private = make_config(instruments=[inst])
    private.dashboard = DashboardSettings(include_positions=False)
    payload = dashboard.build_payload(
        config=private, snapshots=[], signals=[], positions=rows,
        recent_signals=[], problems=[], sources={},
    )
    assert payload["positions"] == [], "positions must not reach a public dashboard"
    assert payload["totals"] == {"withheld": True, "positions": 1}
    text = json.dumps(payload)
    assert "1234.5" not in text and "entry_price" not in text, (
        "no position figure may leak into the published payload"
    )

    public = make_config(instruments=[inst])
    public.dashboard = DashboardSettings(include_positions=True)
    opted_in = dashboard.build_payload(
        config=public, snapshots=[], signals=[], positions=rows,
        recent_signals=[], problems=[], sources={},
    )
    assert len(opted_in["positions"]) == 1, "opting in must publish them"
    assert opted_in["totals"]["value"] == 1234.5


def check_state_rules_fire_on_entry() -> None:
    """Drawdown and 52-week-extreme rules alert on entry, not every day after."""
    rule = Rule(id="dd", kind="drawdown_from_high", level="high",
                params={"lookback": 60, "pct": 20.0})
    inst = instrument(rules=["dd"])

    # Rise to a peak, then fall through -20% and keep falling.
    closes = [100.0 + i for i in range(40)]          # peak 139
    price = closes[-1]
    for _ in range(12):
        price *= 0.97
        closes.append(price)

    fires = 0
    first_fire_at = None
    for end in range(41, len(closes) + 1):
        signals, _ = evaluate(inst, bars_from(closes[:end]), [rule])
        if signals:
            fires += 1
            first_fire_at = first_fire_at or end
    assert fires == 1, (
        f"a drawdown must alert once on entry, not every day it persists "
        f"(fired {fires} times from bar {first_fire_at})"
    )

    # persist = true restores the old continuous behaviour for anyone who wants it.
    sticky = Rule(id="dd", kind="drawdown_from_high", level="high",
                  params={"lookback": 60, "pct": 20.0, "persist": True})
    sticky_fires = sum(
        len(evaluate(inst, bars_from(closes[:end]), [sticky])[0])
        for end in range(41, len(closes) + 1)
    )
    assert sticky_fires > 1, "persist = true should keep firing while true"

    # Same contract for the 52-week-extreme rule.
    near = Rule(id="ne", kind="near_extreme", level="info",
                params={"lookback": 60, "within_pct": 2.0, "side": "high"})
    rising = [100.0 + i * 0.5 for i in range(60)]
    near_fires = sum(
        len(evaluate(instrument(rules=["ne"]), bars_from(rising[:end]), [near])[0])
        for end in range(40, len(rising) + 1)
    )
    assert near_fires <= 1, (
        f"near_extreme fired {near_fires} times while price simply kept rising"
    )


CHECKS: List[Tuple[str, Callable[[], None]]] = [
    ("indicators", check_indicators),
    ("moving-average cross fires once", check_ma_cross_fires_on_the_cross),
    ("rsi threshold fires once", check_rsi_fires_exactly_once),
    ("breakout and volume spike", check_breakout_and_volume),
    ("every rule kind evaluates", check_all_rule_kinds_run),
    ("snapshot fields", check_snapshot),
    ("portfolio stops and targets", check_portfolio),
    ("position sizing arithmetic", check_position_sizing),
    ("cooldowns and alert gates", check_cooldown_and_gates),
    ("quiet hours across midnight", check_quiet_hours),
    ("alert formatting and disclaimer", check_alert_formatting),
    ("provider fallback and cache", check_provider_fallback),
    ("bar parsing edge cases", check_bar_parsing),
    ("dashboard payload", check_dashboard_payload),
    ("position privacy on a public dashboard", check_portfolio_privacy),
    ("state rules fire on entry only", check_state_rules_fire_on_entry),
    ("shipped config validates", check_shipped_config),
]


def run_selftest(verbose: bool = True) -> int:
    failures: List[Tuple[str, str]] = []
    for name, check in CHECKS:
        try:
            check()
        except AssertionError as exc:
            failures.append((name, str(exc) or "assertion failed"))
            print(f"  FAIL  {name}\n        {exc}")
            continue
        except Exception as exc:
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"  ERROR {name}\n        {type(exc).__name__}: {exc}")
            continue
        if verbose:
            print(f"  ok    {name}")

    total = len(CHECKS)
    if failures:
        print(f"\n{len(failures)} of {total} checks failed.")
        return 1
    print(f"\nAll {total} checks passed. The engine works; no network was used.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_selftest())
