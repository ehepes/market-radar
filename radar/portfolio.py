"""Position monitoring and risk arithmetic.

Everything here is mechanical execution of levels *you* wrote into
config/portfolio.toml. The engine does not invent stops, targets or sizes —
it reports when your own numbers are reached and does the arithmetic you would
otherwise do by hand.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from . import indicators as ind
from .config import Config, RiskSettings
from .models import Bar, Holding, Instrument, Signal


def evaluate_holdings(
    config: Config, bars_by_instrument: Dict[str, List[Bar]]
) -> tuple[List[Signal], List[Dict[str, object]]]:
    """Check every open position against its configured exit levels.

    Returns (signals, position_rows) where position_rows feeds the dashboard.
    """
    signals: List[Signal] = []
    rows: List[Dict[str, object]] = []

    for holding in config.holdings:
        inst = config.instrument(holding.instrument_id)
        bars = bars_by_instrument.get(holding.instrument_id) or []
        if inst is None or not bars:
            continue

        closes = [b.close for b in bars]
        price = closes[-1]
        pnl_pct = (price / holding.entry_price) - 1.0 if holding.entry_price else 0.0
        value = price * holding.quantity
        cost = holding.entry_price * holding.quantity

        row: Dict[str, object] = {
            "instrument_id": inst.id,
            "name": inst.name,
            "trade_label": inst.trade_label,
            "quantity": holding.quantity,
            "entry_price": holding.entry_price,
            "price": round(price, 4),
            "value": round(value, 2),
            "cost": round(cost, 2),
            "pnl": round(value - cost, 2),
            "pnl_pct": round(pnl_pct, 4),
            "stop": holding.stop,
            "target": holding.target,
            "as_of": bars[-1].day.isoformat(),
        }

        # --- hard stop -------------------------------------------------
        if holding.stop is not None and price <= holding.stop:
            signals.append(
                Signal(
                    instrument_id=inst.id,
                    rule_id="stop_breached",
                    level="critical",
                    headline=f"STOP BREACHED — {inst.name}",
                    detail=(
                        f"{inst.trade_label} last traded {price:,.2f}, at or below the "
                        f"stop of {holding.stop:,.2f} you set. Position: "
                        f"{holding.quantity:g} units, entry {holding.entry_price:,.2f}, "
                        f"open P/L {pnl_pct * 100:+.1f}% ({value - cost:+,.2f}). "
                        "This is your own exit level being reached — act on it or "
                        "deliberately revise it."
                    ),
                    evidence={
                        "price": round(price, 4),
                        "stop": holding.stop,
                        "pnl_pct": round(pnl_pct, 4),
                    },
                )
            )

        # --- target ----------------------------------------------------
        if holding.target is not None and price >= holding.target:
            signals.append(
                Signal(
                    instrument_id=inst.id,
                    rule_id="target_reached",
                    level="high",
                    headline=f"TARGET REACHED — {inst.name}",
                    detail=(
                        f"{inst.trade_label} last traded {price:,.2f}, at or above your "
                        f"target of {holding.target:,.2f}. Open P/L "
                        f"{pnl_pct * 100:+.1f}% ({value - cost:+,.2f} on "
                        f"{holding.quantity:g} units)."
                    ),
                    evidence={
                        "price": round(price, 4),
                        "target": holding.target,
                        "pnl_pct": round(pnl_pct, 4),
                    },
                )
            )

        # --- trailing stop ---------------------------------------------
        if holding.trailing_stop_pct:
            trail = _trailing_level(closes, holding)
            row["trailing_stop_level"] = round(trail["level"], 4)
            row["peak_since_entry"] = round(trail["peak"], 4)
            if price <= trail["level"]:
                signals.append(
                    Signal(
                        instrument_id=inst.id,
                        rule_id="trailing_stop_breached",
                        level="critical",
                        headline=f"TRAILING STOP HIT — {inst.name}",
                        detail=(
                            f"{inst.trade_label} is {price:,.2f}, which is "
                            f"{holding.trailing_stop_pct:.1f}% or more below its peak of "
                            f"{trail['peak']:,.2f} since you opened the position "
                            f"(trailing level {trail['level']:,.2f}). Open P/L "
                            f"{pnl_pct * 100:+.1f}%."
                        ),
                        evidence={
                            "price": round(price, 4),
                            "peak": round(trail["peak"], 4),
                            "trailing_level": round(trail["level"], 4),
                        },
                    )
                )

        # --- concentration ---------------------------------------------
        if config.risk.account_size > 0:
            weight = value / config.risk.account_size
            row["weight"] = round(weight, 4)
            if weight > config.risk.max_position_pct / 100.0:
                signals.append(
                    Signal(
                        instrument_id=inst.id,
                        rule_id="position_oversized",
                        level="medium",
                        headline=f"Position is {weight * 100:.0f}% of your account",
                        detail=(
                            f"{inst.trade_label} is worth {value:,.2f} "
                            f"{config.risk.account_currency}, i.e. {weight * 100:.1f}% of "
                            f"your stated account size of {config.risk.account_size:,.0f}. "
                            f"Your own cap in config is {config.risk.max_position_pct:.0f}%."
                        ),
                        evidence={"weight": round(weight, 4), "value": round(value, 2)},
                    )
                )

        rows.append(row)

    return signals, rows


def _trailing_level(closes: Sequence[float], holding: Holding) -> Dict[str, float]:
    """Peak close since entry, and the trailing stop implied by it.

    Without an explicit opened date the whole available series is used, which
    is the conservative reading: a higher peak means a higher trailing stop.
    """
    peak = max(closes) if closes else holding.entry_price
    pct = (holding.trailing_stop_pct or 0.0) / 100.0
    return {"peak": peak, "level": peak * (1.0 - pct)}


def position_size(
    risk: RiskSettings,
    price: float,
    stop: Optional[float] = None,
    atr: Optional[float] = None,
    atr_multiple: float = 2.0,
) -> Dict[str, object]:
    """Units implied by your risk budget and your stop distance.

    Pure arithmetic: (account x risk%) / per-unit risk, then capped by your
    maximum position weight. If you supply no stop, an ATR-based distance is
    used so the number means something.
    """
    out: Dict[str, object] = {
        "account_size": risk.account_size,
        "risk_per_trade_pct": risk.risk_per_trade_pct,
        "price": round(price, 4),
    }
    if risk.account_size <= 0 or price <= 0:
        out["error"] = "set risk.account_size in config/radar.toml to size positions"
        return out

    if stop is not None and stop > 0 and stop < price:
        per_unit_risk = price - stop
        out["stop"] = round(stop, 4)
        out["stop_basis"] = "your configured stop"
    elif atr:
        per_unit_risk = atr * atr_multiple
        out["stop"] = round(price - per_unit_risk, 4)
        out["stop_basis"] = f"{atr_multiple:g} x ATR(14) = {per_unit_risk:,.2f}"
    else:
        out["error"] = "need either a stop price or an ATR to measure risk per unit"
        return out

    if per_unit_risk <= 0:
        out["error"] = "stop must sit below the current price for a long position"
        return out

    risk_budget = risk.account_size * (risk.risk_per_trade_pct / 100.0)
    units = risk_budget / per_unit_risk
    notional = units * price

    cap = risk.account_size * (risk.max_position_pct / 100.0)
    capped = notional > cap
    if capped:
        units = cap / price
        notional = units * price

    out.update(
        {
            "risk_budget": round(risk_budget, 2),
            "risk_per_unit": round(per_unit_risk, 4),
            "units": round(units, 6),
            "notional": round(notional, 2),
            "capped_by_max_position": capped,
            "max_position_value": round(cap, 2),
        }
    )
    return out


def portfolio_totals(rows: Sequence[Dict[str, object]], risk: RiskSettings) -> Dict[str, object]:
    """Aggregate open positions for the dashboard header."""
    value = sum(float(r.get("value") or 0.0) for r in rows)
    cost = sum(float(r.get("cost") or 0.0) for r in rows)
    return {
        "positions": len(rows),
        "value": round(value, 2),
        "cost": round(cost, 2),
        "pnl": round(value - cost, 2),
        "pnl_pct": round((value / cost - 1.0), 4) if cost > 0 else None,
        "currency": risk.account_currency,
        "account_size": risk.account_size,
        "cash_implied": round(risk.account_size - value, 2) if risk.account_size else None,
    }


def sizing_for_instrument(
    config: Config, inst: Instrument, bars: Sequence[Bar]
) -> Dict[str, object]:
    """Convenience wrapper used by the CLI's `size` command."""
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    atr_value = ind.last(ind.atr(highs, lows, closes, 14))
    holding = next((h for h in config.holdings if h.instrument_id == inst.id), None)
    return position_size(
        config.risk,
        price=closes[-1],
        stop=holding.stop if holding else None,
        atr=atr_value,
    )
