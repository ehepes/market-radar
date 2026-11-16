"""Alert dispatch: filtering, cooldowns, quiet hours, formatting.

The hard problem with an alerting system is not sending messages, it is *not*
sending them. An engine that pushes every true condition every run gets muted
within a week. So a signal must clear four gates before it reaches your phone:

1. Severity at or above ``notify.min_level``.
2. Not already sent inside ``notify.cooldown_hours`` (escalations pass anyway).
3. Not inside quiet hours, unless its level is in ``always_send_levels``.
4. Inside the per-run budget, highest severity first.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

from ..config import Config
from ..models import LEVELS, Instrument, Signal
from ..state import Store
from .console import ConsoleNotifier
from .pushover import PushoverNotifier
from .telegram import TelegramNotifier

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

DISCLAIMER = (
    "Rule trigger, not advice. You decide whether to act, and you place the "
    "order yourself."
)


class Notifier(Protocol):
    name: str

    def available(self) -> bool: ...
    def describe_missing(self) -> str: ...
    def send(
        self,
        title: str,
        message: str,
        level: str = ...,
        url: Optional[str] = ...,
        url_title: Optional[str] = ...,
    ) -> Dict[str, object]: ...


def build_notifiers(channels: Sequence[str]) -> Tuple[List[Notifier], List[str]]:
    """Instantiate the configured channels, reporting any that can't be used."""
    registry = {
        "pushover": PushoverNotifier,
        "telegram": TelegramNotifier,
        "console": ConsoleNotifier,
    }
    active: List[Notifier] = []
    problems: List[str] = []

    for channel in channels:
        factory = registry.get(channel)
        if factory is None:
            problems.append(f"unknown notify channel '{channel}'")
            continue
        notifier = factory()  # type: ignore[abstract]
        if notifier.available():
            active.append(notifier)
        else:
            problems.append(
                f"channel '{channel}' is configured but not usable "
                f"(missing {notifier.describe_missing()})"
            )

    if not active:
        # Never run blind: fall back to console so the signals still appear
        # in the CI log even when credentials are missing.
        active.append(ConsoleNotifier())
    return active, problems


def _now(tz_name: str) -> datetime:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _parse_hhmm(value: Optional[str]) -> Optional[time]:
    if not value:
        return None
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return None


def in_quiet_hours(config: Config, moment: Optional[datetime] = None) -> bool:
    """True when the clock is inside the configured quiet window.

    Handles windows that wrap midnight (e.g. 23:00 -> 07:00).
    """
    start = _parse_hhmm(config.notify.quiet_start)
    end = _parse_hhmm(config.notify.quiet_end)
    if start is None or end is None:
        return False

    current = (moment or _now(config.engine.timezone)).time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def format_signal(signal: Signal, inst: Optional[Instrument]) -> Tuple[str, str]:
    """Render one signal as (title, body).

    The body always names the instrument you can actually buy, because the
    symbol the signal was computed on is frequently not purchasable from an
    Irish Revolut account.
    """
    name = inst.name if inst else signal.instrument_id
    title = f"{name}: {signal.headline}"

    lines = [signal.detail]
    if inst:
        if inst.is_proxy:
            lines.append("")
            lines.append(
                f"<b>Measured on</b> {inst.signal_symbol} — "
                f"<b>tradeable as</b> {inst.trade_label}"
            )
            if inst.trade_note:
                lines.append(f"<i>{inst.trade_note}</i>")
        elif inst.trade_ticker:
            lines.append("")
            lines.append(f"<b>Instrument:</b> {inst.trade_label}")

    lines.append("")
    lines.append(f"<i>{DISCLAIMER}</i>")
    return title, "\n".join(lines)


def format_digest(items: Sequence[Tuple[Signal, Optional[Instrument]]]) -> Tuple[str, str]:
    """Bundle lower-severity signals into a single message."""
    title = f"Market Radar: {len(items)} signal{'s' if len(items) != 1 else ''}"
    lines: List[str] = []
    for signal, inst in items:
        name = inst.name if inst else signal.instrument_id
        lines.append(f"<b>{name}</b> — {signal.headline}")
        if inst and inst.trade_ticker:
            lines.append(f"   trade as {inst.trade_label}")
    lines.append("")
    lines.append(f"<i>{DISCLAIMER}</i>")
    return title, "\n".join(lines)


def select_for_sending(
    config: Config, store: Store, signals: Sequence[Signal]
) -> Tuple[List[Signal], List[Signal], List[str]]:
    """Apply all four gates.

    Returns (to_send, suppressed, notes).
    """
    min_rank = LEVELS.index(config.notify.min_level)
    quiet = in_quiet_hours(config)
    always = set(config.notify.always_send_levels)

    to_send: List[Signal] = []
    suppressed: List[Signal] = []
    notes: List[str] = []

    # Severity first, so a per-run budget never drops a critical for an info.
    ordered = sorted(signals, key=lambda s: (-s.level_rank, s.instrument_id))

    for signal in ordered:
        if signal.level_rank < min_rank:
            suppressed.append(signal)
            continue
        if not store.should_send(signal, config.notify.cooldown_hours):
            suppressed.append(signal)
            notes.append(
                f"{signal.dedupe_key}: within {config.notify.cooldown_hours}h cooldown"
            )
            continue
        if quiet and signal.level not in always:
            suppressed.append(signal)
            notes.append(f"{signal.dedupe_key}: held back during quiet hours")
            continue
        if len(to_send) >= config.notify.max_alerts_per_run:
            suppressed.append(signal)
            notes.append(f"{signal.dedupe_key}: over per-run alert budget")
            continue
        to_send.append(signal)

    return to_send, suppressed, notes


def dispatch(
    config: Config,
    store: Store,
    signals: Sequence[Signal],
    notifiers: Sequence[Notifier],
    dashboard_url: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, object]:
    """Send the signals that clear the gates, and record what happened."""
    to_send, suppressed, notes = select_for_sending(config, store, signals)

    # High-severity items go out individually so Pushover's priority applies
    # per alert; the rest are bundled so the phone buzzes once.
    individual = [s for s in to_send if s.level in ("high", "critical")]
    digestible = [s for s in to_send if s.level not in ("high", "critical")]

    results: List[Dict[str, object]] = []

    for signal in individual:
        inst = config.instrument(signal.instrument_id)
        title, body = format_signal(signal, inst)
        results.extend(
            _send_all(notifiers, title, body, signal.level, dashboard_url, dry_run)
        )
        if not dry_run:
            store.mark_sent(signal)

    if digestible:
        pairs = [(s, config.instrument(s.instrument_id)) for s in digestible]
        title, body = format_digest(pairs)
        level = max(digestible, key=lambda s: s.level_rank).level
        results.extend(_send_all(notifiers, title, body, level, dashboard_url, dry_run))
        if not dry_run:
            for signal in digestible:
                store.mark_sent(signal)

    if not dry_run:
        sent_keys = {s.dedupe_key for s in to_send}
        for signal in signals:
            store.record_signal(signal, notified=signal.dedupe_key in sent_keys)

    return {
        "sent": len(to_send),
        "suppressed": len(suppressed),
        "notes": notes,
        "results": results,
        "quiet_hours": in_quiet_hours(config),
        "dry_run": dry_run,
    }


def _send_all(
    notifiers: Sequence[Notifier],
    title: str,
    body: str,
    level: str,
    url: Optional[str],
    dry_run: bool,
) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    for notifier in notifiers:
        if dry_run and notifier.name != "console":
            out.append({"channel": notifier.name, "ok": True, "skipped": "dry run"})
            continue
        result = notifier.send(title, body, level=level, url=url)
        result["channel"] = notifier.name
        out.append(result)
    return out


__all__ = [
    "build_notifiers",
    "dispatch",
    "format_digest",
    "format_signal",
    "in_quiet_hours",
    "select_for_sending",
    "DISCLAIMER",
]
