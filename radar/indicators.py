"""Technical indicators, implemented in pure Python.

No numpy/pandas on purpose: this runs unattended on a CI runner, and a
zero-dependency engine cannot be broken by upstream package drift.

Conventions used throughout:
  * Series are plain lists ordered oldest -> newest.
  * Functions that return a series return one the same length as the input,
    with ``None`` in positions where the indicator is not yet defined.
  * ``last()`` pulls the most recent defined value off such a series.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence

Series = List[Optional[float]]


def last(series: Sequence[Optional[float]]) -> Optional[float]:
    """Most recent non-None value, or None if the series never warmed up."""
    for value in reversed(series):
        if value is not None:
            return value
    return None


def _need(values: Sequence[float], period: int) -> bool:
    return period > 0 and len(values) >= period


def sma(values: Sequence[float], period: int) -> Series:
    out: Series = [None] * len(values)
    if not _need(values, period):
        return out
    window = sum(values[:period])
    out[period - 1] = window / period
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out[i] = window / period
    return out


def ema(values: Sequence[float], period: int) -> Series:
    """EMA seeded with the SMA of the first ``period`` points."""
    out: Series = [None] * len(values)
    if not _need(values, period):
        return out
    alpha = 2.0 / (period + 1.0)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = (values[i] - prev) * alpha + prev
        out[i] = prev
    return out


def stdev(values: Sequence[float], period: int) -> Series:
    """Rolling population standard deviation."""
    out: Series = [None] * len(values)
    if not _need(values, period):
        return out
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        var = sum((v - mean) ** 2 for v in window) / period
        out[i] = math.sqrt(var)
    return out


def rsi(values: Sequence[float], period: int = 14) -> Series:
    """Relative Strength Index using Wilder's smoothing."""
    out: Series = [None] * len(values)
    if len(values) <= period:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> Series:
    out: Series = [None] * len(closes)
    for i in range(1, len(closes)):
        prev_close = closes[i - 1]
        out[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - prev_close),
            abs(lows[i] - prev_close),
        )
    return out


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> Series:
    """Average True Range using Wilder's smoothing."""
    out: Series = [None] * len(closes)
    tr = true_range(highs, lows, closes)
    usable = [v for v in tr if v is not None]
    if len(usable) < period:
        return out

    prev = sum(usable[:period]) / period
    out[period] = prev
    for i in range(period + 1, len(closes)):
        value = tr[i]
        if value is None:
            continue
        prev = (prev * (period - 1) + value) / period
        out[i] = prev
    return out


def macd(
    values: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[Series, Series, Series]:
    """Returns (macd_line, signal_line, histogram)."""
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    line: Series = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_ema, slow_ema)
    ]

    defined = [(i, v) for i, v in enumerate(line) if v is not None]
    sig: Series = [None] * len(values)
    hist: Series = [None] * len(values)
    if len(defined) >= signal:
        sig_vals = ema([v for _, v in defined], signal)
        for (idx, _), s in zip(defined, sig_vals):
            sig[idx] = s
    for i in range(len(values)):
        if line[i] is not None and sig[i] is not None:
            hist[i] = line[i] - sig[i]
    return line, sig, hist


def bollinger(
    values: Sequence[float], period: int = 20, mult: float = 2.0
) -> tuple[Series, Series, Series]:
    """Returns (upper, middle, lower)."""
    mid = sma(values, period)
    sd = stdev(values, period)
    upper: Series = [None] * len(values)
    lower: Series = [None] * len(values)
    for i in range(len(values)):
        if mid[i] is not None and sd[i] is not None:
            upper[i] = mid[i] + mult * sd[i]
            lower[i] = mid[i] - mult * sd[i]
    return upper, mid, lower


def donchian(
    highs: Sequence[float], lows: Sequence[float], period: int = 20
) -> tuple[Series, Series]:
    """Rolling highest high and lowest low, EXCLUDING the current bar.

    Excluding today is what makes a breakout test meaningful: 'today closed
    above the prior 20-day high' rather than the tautological 'today's high
    is within the window containing today'.
    """
    hi: Series = [None] * len(highs)
    lo: Series = [None] * len(lows)
    for i in range(period, len(highs)):
        hi[i] = max(highs[i - period : i])
        lo[i] = min(lows[i - period : i])
    return hi, lo


def zscore(values: Sequence[float], period: int) -> Series:
    """How many standard deviations the current value sits from its mean."""
    out: Series = [None] * len(values)
    means = sma(values, period)
    sds = stdev(values, period)
    for i in range(len(values)):
        if means[i] is None or sds[i] is None or sds[i] == 0:
            continue
        out[i] = (values[i] - means[i]) / sds[i]
    return out


def returns(values: Sequence[float]) -> Series:
    """Simple period-over-period returns as fractions."""
    out: Series = [None] * len(values)
    for i in range(1, len(values)):
        if values[i - 1]:
            out[i] = (values[i] / values[i - 1]) - 1.0
    return out


def realized_vol(values: Sequence[float], period: int = 20, periods_per_year: int = 252) -> Series:
    """Annualised volatility from log returns."""
    logret: List[Optional[float]] = [None] * len(values)
    for i in range(1, len(values)):
        if values[i - 1] > 0 and values[i] > 0:
            logret[i] = math.log(values[i] / values[i - 1])

    out: Series = [None] * len(values)
    scale = math.sqrt(periods_per_year)
    for i in range(len(values)):
        window = [v for v in logret[max(0, i - period + 1) : i + 1] if v is not None]
        if len(window) < period:
            continue
        mean = sum(window) / len(window)
        var = sum((v - mean) ** 2 for v in window) / len(window)
        out[i] = math.sqrt(var) * scale
    return out


def pct_from_extreme(values: Sequence[float], period: int, high: bool = True) -> Optional[float]:
    """Current value's distance from its period high/low, as a fraction.

    Negative when below a high; positive when above a low.
    """
    if not values:
        return None
    window = values[-period:] if period < len(values) else list(values)
    if not window:
        return None
    reference = max(window) if high else min(window)
    if reference == 0:
        return None
    return (values[-1] / reference) - 1.0


def crossed_up(fast: Sequence[Optional[float]], slow: Sequence[Optional[float]]) -> bool:
    """True when fast crossed above slow on the latest bar."""
    return _cross(fast, slow, up=True)


def crossed_down(fast: Sequence[Optional[float]], slow: Sequence[Optional[float]]) -> bool:
    """True when fast crossed below slow on the latest bar."""
    return _cross(fast, slow, up=False)


def _cross(fast: Sequence[Optional[float]], slow: Sequence[Optional[float]], up: bool) -> bool:
    if len(fast) < 2 or len(slow) < 2:
        return False
    f_now, f_prev = fast[-1], fast[-2]
    s_now, s_prev = slow[-1], slow[-2]
    if None in (f_now, f_prev, s_now, s_prev):
        return False
    if up:
        return f_prev <= s_prev and f_now > s_now
    return f_prev >= s_prev and f_now < s_now
