"""The rule engine: turns bars + configured rules into Signals.

Design notes
------------
* Every rule is *descriptive*. A signal states what the market did ("50-day
  crossed above the 200-day, RSI 61"), never what you should do about it.
  The decision is yours; this is a tripwire, not an oracle.
* Indicators are memoised per instrument run, so twelve rules on one symbol
  cost one pass over the data.
* An unknown rule kind is reported loudly rather than silently skipped —
  a typo in rules.toml should be obvious, not invisible.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence

from . import indicators as ind
from .models import Bar, Instrument, Rule, Signal


class Context:
    """Memoised view of one instrument's price history."""

    def __init__(
        self,
        inst: Instrument,
        bars: Sequence[Bar],
        events: Optional[Sequence[object]] = None,
    ) -> None:
        self.inst = inst
        self.bars = list(bars)
        # Scheduled events the price series cannot reveal. The soonest one is
        # what rules care about, and what enriches every other alert.
        self.events = list(events or [])
        self.next_earnings = self.events[0] if self.events else None
        self.closes = [b.close for b in self.bars]
        self.highs = [b.high for b in self.bars]
        self.lows = [b.low for b in self.bars]
        self.volumes = [b.volume for b in self.bars]
        self._cache: Dict[str, object] = {}

    # ------------------------------------------------------------ helpers

    @property
    def close(self) -> float:
        return self.closes[-1]

    @property
    def last_day(self) -> str:
        return self.bars[-1].day.isoformat()

    def _memo(self, key: str, factory: Callable[[], object]) -> object:
        if key not in self._cache:
            self._cache[key] = factory()
        return self._cache[key]

    def sma(self, period: int) -> ind.Series:
        return self._memo(f"sma{period}", lambda: ind.sma(self.closes, period))  # type: ignore[return-value]

    def ema(self, period: int) -> ind.Series:
        return self._memo(f"ema{period}", lambda: ind.ema(self.closes, period))  # type: ignore[return-value]

    def rsi(self, period: int) -> ind.Series:
        return self._memo(f"rsi{period}", lambda: ind.rsi(self.closes, period))  # type: ignore[return-value]

    def atr(self, period: int) -> ind.Series:
        return self._memo(
            f"atr{period}", lambda: ind.atr(self.highs, self.lows, self.closes, period)
        )  # type: ignore[return-value]

    def macd(self, fast: int, slow: int, signal: int):
        return self._memo(
            f"macd{fast}-{slow}-{signal}",
            lambda: ind.macd(self.closes, fast, slow, signal),
        )

    def bollinger(self, period: int, mult: float):
        return self._memo(
            f"bb{period}-{mult}", lambda: ind.bollinger(self.closes, period, mult)
        )

    def donchian(self, period: int):
        return self._memo(
            f"dc{period}", lambda: ind.donchian(self.highs, self.lows, period)
        )

    def volume_z(self, period: int) -> ind.Series:
        return self._memo(
            f"volz{period}", lambda: ind.zscore(self.volumes, period)
        )  # type: ignore[return-value]

    def realized_vol(self, period: int) -> ind.Series:
        return self._memo(
            f"rvol{period}", lambda: ind.realized_vol(self.closes, period)
        )  # type: ignore[return-value]


def _fmt(value: Optional[float], digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def _pct(value: Optional[float], digits: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100:+.{digits}f}%"


def _signal(ctx: Context, rule: Rule, headline: str, detail: str, **evidence) -> Signal:
    base = {"close": round(ctx.close, 4), "as_of": ctx.last_day}
    base.update({k: (round(v, 4) if isinstance(v, float) else v) for k, v in evidence.items()})
    return Signal(
        instrument_id=ctx.inst.id,
        rule_id=rule.id,
        level=rule.level,
        headline=headline,
        detail=detail,
        evidence=base,
    )


# --------------------------------------------------------------- evaluators


def eval_ma_cross(ctx: Context, rule: Rule) -> Optional[Signal]:
    fast_p = int(rule.param("fast", 50))
    slow_p = int(rule.param("slow", 200))
    direction = str(rule.param("direction", "both")).lower()
    use_ema = bool(rule.param("use_ema", False))

    fast = ctx.ema(fast_p) if use_ema else ctx.sma(fast_p)
    slow = ctx.ema(slow_p) if use_ema else ctx.sma(slow_p)
    kind = "EMA" if use_ema else "SMA"

    if direction in ("up", "both") and ind.crossed_up(fast, slow):
        return _signal(
            ctx,
            rule,
            f"{fast_p}-day crossed ABOVE {slow_p}-day",
            f"The {fast_p}-day {kind} ({_fmt(ind.last(fast))}) crossed above the "
            f"{slow_p}-day {kind} ({_fmt(ind.last(slow))}). Last close {_fmt(ctx.close)}. "
            "Conventionally read as a shift to an uptrend; it is a lagging measure "
            "and says nothing about what happens next.",
            fast=ind.last(fast),
            slow=ind.last(slow),
            direction="up",
        )
    if direction in ("down", "both") and ind.crossed_down(fast, slow):
        return _signal(
            ctx,
            rule,
            f"{fast_p}-day crossed BELOW {slow_p}-day",
            f"The {fast_p}-day {kind} ({_fmt(ind.last(fast))}) crossed below the "
            f"{slow_p}-day {kind} ({_fmt(ind.last(slow))}). Last close {_fmt(ctx.close)}. "
            "Conventionally read as a shift to a downtrend.",
            fast=ind.last(fast),
            slow=ind.last(slow),
            direction="down",
        )
    return None


def eval_rsi(ctx: Context, rule: Rule) -> Optional[Signal]:
    period = int(rule.param("period", 14))
    below = rule.param("below")
    above = rule.param("above")
    series = ctx.rsi(period)
    value = ind.last(series)
    if value is None:
        return None

    # Only fire on the crossing bar, so a month-long oversold reading does not
    # re-alert every run.
    previous = series[-2] if len(series) > 1 else None

    if below is not None and value < float(below):
        if previous is not None and previous < float(below):
            return None
        return _signal(
            ctx,
            rule,
            f"RSI({period}) fell below {below} — {value:.1f}",
            f"RSI({period}) is {value:.1f}, under your {below} threshold, at a close of "
            f"{_fmt(ctx.close)}. Commonly described as oversold; in a downtrend RSI can "
            "stay low for weeks.",
            rsi=value,
            threshold=float(below),
        )
    if above is not None and value > float(above):
        if previous is not None and previous > float(above):
            return None
        return _signal(
            ctx,
            rule,
            f"RSI({period}) rose above {above} — {value:.1f}",
            f"RSI({period}) is {value:.1f}, over your {above} threshold, at a close of "
            f"{_fmt(ctx.close)}. Commonly described as overbought.",
            rsi=value,
            threshold=float(above),
        )
    return None


def eval_breakout(ctx: Context, rule: Rule) -> Optional[Signal]:
    period = int(rule.param("period", 20))
    direction = str(rule.param("direction", "up")).lower()
    hi, lo = ctx.donchian(period)
    high_ref, low_ref = ind.last(hi), ind.last(lo)

    if direction == "up" and high_ref is not None and ctx.close > high_ref:
        return _signal(
            ctx,
            rule,
            f"Broke above its {period}-day high",
            f"Close {_fmt(ctx.close)} is above the prior {period}-day high of "
            f"{_fmt(high_ref)} ({_pct(ctx.close / high_ref - 1)}).",
            breakout_level=high_ref,
            direction="up",
        )
    if direction == "down" and low_ref is not None and ctx.close < low_ref:
        return _signal(
            ctx,
            rule,
            f"Broke below its {period}-day low",
            f"Close {_fmt(ctx.close)} is below the prior {period}-day low of "
            f"{_fmt(low_ref)} ({_pct(ctx.close / low_ref - 1)}).",
            breakout_level=low_ref,
            direction="down",
        )
    return None


def eval_pullback_to_ma(ctx: Context, rule: Rule) -> Optional[Signal]:
    """Price touches a rising moving average — a classic trend-following entry zone."""
    period = int(rule.param("period", 50))
    tolerance = float(rule.param("tolerance_pct", 2.0)) / 100.0
    trend_period = int(rule.param("trend_period", 200))

    ma = ctx.sma(period)
    ma_now = ind.last(ma)
    if ma_now is None:
        return None

    distance = (ctx.close / ma_now) - 1.0
    if abs(distance) > tolerance:
        return None

    # Only interesting while the longer trend is still up.
    if trend_period:
        trend = ctx.sma(trend_period)
        trend_now = ind.last(trend)
        if trend_now is None or ctx.close < trend_now:
            return None

    return _signal(
        ctx,
        rule,
        f"Pulled back to its {period}-day average",
        f"Close {_fmt(ctx.close)} is within {tolerance * 100:.1f}% of the {period}-day "
        f"SMA ({_fmt(ma_now)}, {_pct(distance)}) while still above its {trend_period}-day "
        "average.",
        ma=ma_now,
        distance=distance,
    )


def eval_drawdown_from_high(ctx: Context, rule: Rule) -> Optional[Signal]:
    """Fires when price *enters* a drawdown, not every day it stays in one.

    A drawdown is a lasting state, not an event: "down 60% from its high" can
    stay true for a year. Alerting on the state would re-fire every cooldown
    window until you muted the app, so this reports the crossing instead —
    the same semantics as the RSI and volatility rules. The current depth is
    always on the dashboard; the alert is for the moment it changes.

    Set ``persist = true`` on the rule to get the old state behaviour.
    """
    lookback = int(rule.param("lookback", 252))
    threshold = float(rule.param("pct", 10.0)) / 100.0
    drop = ind.pct_from_extreme(ctx.closes, lookback, high=True)
    if drop is None or drop > -threshold:
        return None

    if not bool(rule.param("persist", False)) and len(ctx.closes) > 1:
        previous = ind.pct_from_extreme(ctx.closes[:-1], lookback, high=True)
        if previous is not None and previous <= -threshold:
            return None

    window = ctx.closes[-lookback:]
    return _signal(
        ctx,
        rule,
        f"Down {abs(drop) * 100:.1f}% from its {lookback}-day high",
        f"Close {_fmt(ctx.close)} is {_pct(drop)} below the {lookback}-day high of "
        f"{_fmt(max(window))}. Your threshold was {threshold * 100:.0f}%.",
        drawdown=drop,
        period_high=max(window),
    )


def eval_volume_spike(ctx: Context, rule: Rule) -> Optional[Signal]:
    period = int(rule.param("period", 20))
    threshold = float(rule.param("z", 2.5))
    series = ctx.volume_z(period)
    value = ind.last(series)
    if value is None or value < threshold:
        return None
    avg = ind.last(ind.sma(ctx.volumes, period))
    change = ind.last(ind.returns(ctx.closes))
    return _signal(
        ctx,
        rule,
        f"Unusual volume — {value:.1f} sigma above normal",
        f"Volume {ctx.volumes[-1]:,.0f} vs a {period}-day average of {avg:,.0f} "
        f"({value:.1f} standard deviations). Price moved {_pct(change)} on the day. "
        "Volume spikes often mark news; check what happened before reading anything "
        "into the price.",
        volume_z=value,
        volume=ctx.volumes[-1],
        day_change=change,
    )


def eval_pct_move(ctx: Context, rule: Rule) -> Optional[Signal]:
    days = int(rule.param("days", 1))
    threshold = float(rule.param("pct", 5.0)) / 100.0
    direction = str(rule.param("direction", "both")).lower()
    if len(ctx.closes) <= days:
        return None

    start = ctx.closes[-(days + 1)]
    if start <= 0:
        return None
    change = (ctx.close / start) - 1.0

    window = "on the day" if days == 1 else f"over {days} sessions"
    if direction in ("up", "both") and change >= threshold:
        return _signal(
            ctx,
            rule,
            f"Up {change * 100:.1f}% {window}",
            f"Close moved from {_fmt(start)} to {_fmt(ctx.close)} ({_pct(change)}) "
            f"{window}, past your {threshold * 100:.1f}% threshold.",
            change=change,
            days=days,
        )
    if direction in ("down", "both") and change <= -threshold:
        return _signal(
            ctx,
            rule,
            f"Down {abs(change) * 100:.1f}% {window}",
            f"Close moved from {_fmt(start)} to {_fmt(ctx.close)} ({_pct(change)}) "
            f"{window}, past your {threshold * 100:.1f}% threshold.",
            change=change,
            days=days,
        )
    return None


def eval_macd_cross(ctx: Context, rule: Rule) -> Optional[Signal]:
    fast = int(rule.param("fast", 12))
    slow = int(rule.param("slow", 26))
    signal_p = int(rule.param("signal", 9))
    direction = str(rule.param("direction", "both")).lower()
    line, sig, _hist = ctx.macd(fast, slow, signal_p)

    if direction in ("up", "both") and ind.crossed_up(line, sig):
        return _signal(
            ctx,
            rule,
            "MACD crossed above its signal line",
            f"MACD ({_fmt(ind.last(line), 3)}) crossed above the signal line "
            f"({_fmt(ind.last(sig), 3)}) at a close of {_fmt(ctx.close)}.",
            macd=ind.last(line),
            signal=ind.last(sig),
            direction="up",
        )
    if direction in ("down", "both") and ind.crossed_down(line, sig):
        return _signal(
            ctx,
            rule,
            "MACD crossed below its signal line",
            f"MACD ({_fmt(ind.last(line), 3)}) crossed below the signal line "
            f"({_fmt(ind.last(sig), 3)}) at a close of {_fmt(ctx.close)}.",
            macd=ind.last(line),
            signal=ind.last(sig),
            direction="down",
        )
    return None


def eval_bollinger_break(ctx: Context, rule: Rule) -> Optional[Signal]:
    period = int(rule.param("period", 20))
    mult = float(rule.param("mult", 2.0))
    direction = str(rule.param("direction", "both")).lower()
    upper, mid, lower = ctx.bollinger(period, mult)
    up, md, lo = ind.last(upper), ind.last(mid), ind.last(lower)
    if None in (up, md, lo):
        return None

    if direction in ("up", "both") and ctx.close > up:
        return _signal(
            ctx,
            rule,
            f"Closed above its upper Bollinger band ({period}, {mult}σ)",
            f"Close {_fmt(ctx.close)} is above the upper band {_fmt(up)} "
            f"(mid {_fmt(md)}).",
            upper=up,
            middle=md,
            direction="up",
        )
    if direction in ("down", "both") and ctx.close < lo:
        return _signal(
            ctx,
            rule,
            f"Closed below its lower Bollinger band ({period}, {mult}σ)",
            f"Close {_fmt(ctx.close)} is below the lower band {_fmt(lo)} "
            f"(mid {_fmt(md)}).",
            lower=lo,
            middle=md,
            direction="down",
        )
    return None


def eval_vol_regime(ctx: Context, rule: Rule) -> Optional[Signal]:
    period = int(rule.param("period", 20))
    threshold = float(rule.param("above_pct", 40.0)) / 100.0
    series = ctx.realized_vol(period)
    value = ind.last(series)
    if value is None or value < threshold:
        return None
    previous = series[-2] if len(series) > 1 else None
    if previous is not None and previous >= threshold:
        return None
    return _signal(
        ctx,
        rule,
        f"Volatility jumped to {value * 100:.0f}% annualised",
        f"{period}-day realised volatility is {value * 100:.0f}%, above your "
        f"{threshold * 100:.0f}% threshold. Position sizes calibrated in calmer "
        "conditions carry more risk than they did.",
        realized_vol=value,
        threshold=threshold,
    )


def eval_near_extreme(ctx: Context, rule: Rule) -> Optional[Signal]:
    """Fires on entering the zone near a 52-week extreme, not while sitting in it.

    Same reasoning as the drawdown rule: an instrument can hug its high for
    weeks. ``persist = true`` restores the continuous behaviour.
    """
    lookback = int(rule.param("lookback", 252))
    within = float(rule.param("within_pct", 2.0)) / 100.0
    side = str(rule.param("side", "high")).lower()

    distance = ind.pct_from_extreme(ctx.closes, lookback, high=(side == "high"))
    if distance is None:
        return None

    if not bool(rule.param("persist", False)) and len(ctx.closes) > 1:
        previous = ind.pct_from_extreme(ctx.closes[:-1], lookback, high=(side == "high"))
        if previous is not None:
            already_in_zone = (
                previous >= -within if side == "high" else previous <= within
            )
            if already_in_zone:
                return None

    if side == "high" and distance >= -within:
        window = ctx.closes[-lookback:]
        return _signal(
            ctx,
            rule,
            f"Within {within * 100:.0f}% of its {lookback}-day high",
            f"Close {_fmt(ctx.close)} vs a {lookback}-day high of {_fmt(max(window))} "
            f"({_pct(distance)}).",
            distance=distance,
            extreme=max(window),
        )
    if side == "low" and distance <= within:
        window = ctx.closes[-lookback:]
        return _signal(
            ctx,
            rule,
            f"Within {within * 100:.0f}% of its {lookback}-day low",
            f"Close {_fmt(ctx.close)} vs a {lookback}-day low of {_fmt(min(window))} "
            f"({_pct(distance)}).",
            distance=distance,
            extreme=min(window),
        )
    return None


def eval_earnings_soon(ctx: Context, rule: Rule) -> Optional[Signal]:
    """Fires when a company is about to report.

    This is the rule that answers "what should I watch today" — a scheduled
    event is a far better reason to pay attention than any indicator reading,
    because it is a known unknown with a known time.
    """
    within = int(rule.param("within_days", 1))
    event = ctx.next_earnings
    if event is None:
        return None

    days = event.days_away()
    if days < 0 or days > within:
        return None

    rsi = ind.last(ctx.rsi(14))
    drop = ind.pct_from_extreme(ctx.closes, 252, high=True)
    context_bits = []
    if rsi is not None:
        context_bits.append(f"RSI {rsi:.0f}")
    if drop is not None:
        context_bits.append(f"{_pct(drop)} from its 52-week high")
    context = ", ".join(context_bits)

    # describe() is a full clause ("reports TOMORROW after the US close") meant
    # to follow a company name, so the headline builds its own phrasing rather
    # than prefixing it and reading "Reports earnings reports TOMORROW".
    if days == 0:
        headline = f"Reports earnings TODAY {event.when_text}"
    elif days == 1:
        headline = f"Reports earnings TOMORROW {event.when_text}"
    else:
        headline = f"Reports earnings in {days} days ({event.day.isoformat()})"
    detail = (
        f"{ctx.inst.name} {event.describe()}. Last close {_fmt(ctx.close)}"
        + (f" ({context})." if context else ".")
        + (f" Consensus EPS {event.eps_forecast}." if event.eps_forecast else "")
        + " Results are a scheduled unknown: moves around them are often large "
        "in both directions, and a beat can still sell off."
    )
    return _signal(
        ctx,
        rule,
        headline,
        detail,
        earnings_date=event.day.isoformat(),
        when=event.when,
        days_away=days,
    )


EVALUATORS: Dict[str, Callable[[Context, Rule], Optional[Signal]]] = {
    "ma_cross": eval_ma_cross,
    "rsi": eval_rsi,
    "breakout": eval_breakout,
    "pullback_to_ma": eval_pullback_to_ma,
    "drawdown_from_high": eval_drawdown_from_high,
    "volume_spike": eval_volume_spike,
    "pct_move": eval_pct_move,
    "macd_cross": eval_macd_cross,
    "bollinger_break": eval_bollinger_break,
    "vol_regime": eval_vol_regime,
    "near_extreme": eval_near_extreme,
    "earnings_soon": eval_earnings_soon,
}


class UnknownRuleKind(KeyError):
    """rules.toml names a rule kind the engine does not implement."""


def evaluate(
    inst: Instrument,
    bars: Sequence[Bar],
    rules: Sequence[Rule],
    events: Optional[Sequence[object]] = None,
) -> tuple[List[Signal], List[str]]:
    """Run every rule against one instrument.

    Returns (signals, problems). Problems are config or data issues worth
    surfacing rather than swallowing.
    """
    problems: List[str] = []
    if not bars:
        return [], [f"{inst.id}: no price data"]

    ctx = Context(inst, bars, events)
    signals: List[Signal] = []

    for rule in rules:
        evaluator = EVALUATORS.get(rule.kind)
        if evaluator is None:
            problems.append(
                f"{inst.id}: rule '{rule.id}' has unknown kind '{rule.kind}' "
                f"(known kinds: {', '.join(sorted(EVALUATORS))})"
            )
            continue
        try:
            signal = evaluator(ctx, rule)
        except Exception as exc:  # one bad rule must not abort the run
            problems.append(f"{inst.id}/{rule.id}: {type(exc).__name__}: {exc}")
            continue
        if signal:
            signals.append(signal)

    # A scheduled report changes how every other signal on that instrument
    # should be read: a 5% drop two days before earnings is a different thing
    # from a 5% drop in a quiet week. Rather than make you cross-reference,
    # every alert for that instrument carries the date.
    event = ctx.next_earnings
    if event is not None and 0 <= event.days_away() <= 3:
        note = f" Note: {inst.name} {event.describe()}."
        for signal in signals:
            if signal.evidence.get("earnings_date"):
                continue  # the earnings rule already says this
            signal.detail += note
            signal.evidence["earnings_date"] = event.day.isoformat()

    return signals, problems


def snapshot(
    inst: Instrument,
    bars: Sequence[Bar],
    events: Optional[Sequence[object]] = None,
) -> Dict[str, object]:
    """Per-instrument metrics for the dashboard, independent of any rule."""
    if not bars:
        return {"instrument_id": inst.id, "status": "no data"}

    ctx = Context(inst, bars, events)
    change_1d = ind.last(ind.returns(ctx.closes))
    return {
        "instrument_id": inst.id,
        "name": inst.name,
        "asset_class": inst.asset_class,
        "signal_symbol": inst.signal_symbol,
        "trade_label": inst.trade_label,
        "trade_ticker": inst.trade_ticker,
        "trade_isin": inst.trade_isin,
        "trade_venue": inst.trade_venue,
        "trade_note": inst.trade_note,
        "is_proxy": inst.is_proxy,
        "as_of": ctx.last_day,
        "close": round(ctx.close, 4),
        "change_1d": _round(change_1d),
        "change_5d": _round(_change_over(ctx.closes, 5)),
        "change_21d": _round(_change_over(ctx.closes, 21)),
        "sma50": _round(ind.last(ctx.sma(50))),
        "sma200": _round(ind.last(ctx.sma(200))),
        "rsi14": _round(ind.last(ctx.rsi(14))),
        "atr14": _round(ind.last(ctx.atr(14))),
        "realized_vol_20": _round(ind.last(ctx.realized_vol(20))),
        "from_252d_high": _round(ind.pct_from_extreme(ctx.closes, 252, high=True)),
        "bars": len(bars),
        "earnings_date": (
            ctx.next_earnings.day.isoformat() if ctx.next_earnings else None
        ),
        "earnings_when": (
            ctx.next_earnings.describe() if ctx.next_earnings else None
        ),
    }


def _change_over(closes: Sequence[float], days: int) -> Optional[float]:
    if len(closes) <= days or closes[-(days + 1)] <= 0:
        return None
    return (closes[-1] / closes[-(days + 1)]) - 1.0


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(value, digits)
