"""Command-line entry point.

  python3 -m radar check            validate config, no network calls
  python3 -m radar run              full cycle: fetch, evaluate, alert, publish
  python3 -m radar run --dry-run    everything except sending real alerts
  python3 -m radar selftest         offline end-to-end test on synthetic data
  python3 -m radar test-alert       prove your Pushover keys work
  python3 -m radar size <id>        position size from your risk settings
  python3 -m radar snapshot         print a table of current readings
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .config import Config, ConfigError, load_config
from .models import Bar, Signal
from .notify import build_notifiers, dispatch, in_quiet_hours
from .portfolio import evaluate_holdings, sizing_for_instrument
from .providers import MarketData
from .signals import EVALUATORS, evaluate, snapshot
from .state import Store
from . import dashboard


def _load(args: argparse.Namespace) -> Config:
    try:
        return load_config(Path(args.config_dir))
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


# --------------------------------------------------------------------- check


def cmd_check(args: argparse.Namespace) -> int:
    config = _load(args)
    enabled = [i for i in config.instruments if i.enabled]

    print("Config is valid.\n")
    print(f"  Instruments : {len(enabled)} enabled ({len(config.instruments)} total)")
    print(f"  Rules       : {len(config.rules)}")
    print(f"  Holdings    : {len(config.holdings)}")
    print(f"  Channels    : {', '.join(config.notify.channels)}")
    print(f"  Min level   : {config.notify.min_level}")
    print(f"  Cooldown    : {config.notify.cooldown_hours}h")
    print(f"  Timezone    : {config.engine.timezone}")

    quiet = "none"
    if config.notify.quiet_start and config.notify.quiet_end:
        quiet = f"{config.notify.quiet_start}-{config.notify.quiet_end}"
        quiet += " (now: inside)" if in_quiet_hours(config) else " (now: outside)"
    print(f"  Quiet hours : {quiet}")

    kinds = {r.kind for r in config.rules.values()}
    unknown = kinds - set(EVALUATORS)
    if unknown:
        print(f"\nUnknown rule kinds: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 1

    notifiers, problems = build_notifiers(config.notify.channels)
    print(f"\nActive notifiers: {', '.join(n.name for n in notifiers)}")
    for problem in problems:
        print(f"  ! {problem}")

    proxies = [i for i in enabled if i.is_proxy]
    if proxies:
        print(f"\nProxy mappings ({len(proxies)}) — signal symbol -> what you buy:")
        for inst in proxies:
            print(f"  {inst.signal_symbol:<10} -> {inst.trade_label}")

    missing_keys = [
        key
        for key in ("TWELVEDATA_API_KEY",)
        if not os.environ.get(key)
    ]
    if missing_keys:
        print(
            f"\nNote: {', '.join(missing_keys)} not set — equity/ETF data will "
            "fall back to Yahoo, which is frequently rate-limited.",
        )
    return 0


# ----------------------------------------------------------------------- run


def _fetch_all(config: Config, store: Store) -> tuple[MarketData, Dict[str, List[Bar]], List[str]]:
    market = MarketData(cache=store)
    bars_by_instrument: Dict[str, List[Bar]] = {}
    problems: List[str] = []

    for inst in config.instruments:
        if not inst.enabled:
            continue
        bars = market.bars(inst, config.engine.history_days)
        if not bars:
            problems.append(
                f"{inst.id} ({inst.signal_symbol}): no data — "
                + "; ".join(market.errors.get(inst.id, ["unknown reason"]))
            )
            continue
        if len(bars) < config.engine.min_bars:
            problems.append(
                f"{inst.id}: only {len(bars)} bars, need {config.engine.min_bars}; "
                "long-window rules will stay quiet"
            )
        bars_by_instrument[inst.id] = bars

    return market, bars_by_instrument, problems


def cmd_run(args: argparse.Namespace) -> int:
    config = _load(args)
    store = Store(args.db)

    if args.force:
        cleared = store.clear_cooldowns()
        print(f"Cleared {cleared} alert cooldown(s).")

    market, bars_by_instrument, problems = _fetch_all(config, store)
    print(
        f"Fetched {len(bars_by_instrument)} / "
        f"{len([i for i in config.instruments if i.enabled])} instruments."
    )

    signals: List[Signal] = []
    snapshots: List[Dict[str, object]] = []

    for inst in config.instruments:
        if not inst.enabled:
            continue
        bars = bars_by_instrument.get(inst.id)
        if not bars:
            continue
        fired, rule_problems = evaluate(inst, bars, config.rules_for(inst))
        signals.extend(fired)
        problems.extend(rule_problems)
        snap = snapshot(inst, bars)
        snap["source"] = market.sources.get(inst.id, "unknown")
        snapshots.append(snap)

    position_signals, positions = evaluate_holdings(config, bars_by_instrument)
    signals.extend(position_signals)

    print(f"Rules fired: {len(signals)} signal(s).")
    for problem in problems:
        print(f"  ! {problem}", file=sys.stderr)

    notifiers, notify_problems = build_notifiers(config.notify.channels)
    for problem in notify_problems:
        print(f"  ! {problem}", file=sys.stderr)
    problems.extend(notify_problems)

    outcome = dispatch(
        config,
        store,
        signals,
        notifiers,
        dashboard_url=args.dashboard_url or os.environ.get("RADAR_DASHBOARD_URL"),
        dry_run=args.dry_run,
    )
    print(
        f"Alerts: {outcome['sent']} sent, {outcome['suppressed']} suppressed"
        + (" (dry run)" if args.dry_run else "")
    )
    for note in outcome["notes"]:  # type: ignore[index]
        print(f"  - {note}")

    for result in outcome["results"]:  # type: ignore[index]
        if not result.get("ok"):
            print(
                f"  ! delivery failed via {result.get('channel')}: "
                f"{result.get('error')}",
                file=sys.stderr,
            )

    payload = dashboard.build_payload(
        config=config,
        snapshots=snapshots,
        signals=signals,
        positions=positions,
        recent_signals=store.recent_signals(60),
        problems=problems,
        sources=market.sources,
        sparklines=dashboard.sparkline_data(bars_by_instrument),
    )
    written = dashboard.write(payload, args.out)
    print(f"Dashboard data written to {written}")

    store.prune_bars()
    store.record_run(
        {
            "instruments": len(bars_by_instrument),
            "signals": len(signals),
            "sent": outcome["sent"],
            "problems": len(problems),
        }
    )
    store.close()

    # A failure to fetch anything at all is a real failure; partial is not.
    return 1 if not bars_by_instrument else 0


# ------------------------------------------------------------------ snapshot


def cmd_snapshot(args: argparse.Namespace) -> int:
    config = _load(args)
    store = Store(args.db)
    market, bars_by_instrument, problems = _fetch_all(config, store)

    header = f"{'Instrument':<26} {'Close':>10} {'1d':>8} {'21d':>8} {'RSI':>6} {'vs 52wk hi':>11}  Source"
    print(header)
    print("-" * len(header))
    for inst in config.instruments:
        bars = bars_by_instrument.get(inst.id)
        if not bars:
            continue
        snap = snapshot(inst, bars)
        print(
            f"{str(snap['name'])[:25]:<26} "
            f"{_num(snap['close']):>10} "
            f"{_pct(snap['change_1d']):>8} "
            f"{_pct(snap['change_21d']):>8} "
            f"{_num(snap['rsi14'], 1):>6} "
            f"{_pct(snap['from_252d_high']):>11}  "
            f"{market.sources.get(inst.id, '?')}"
        )
    for problem in problems:
        print(f"  ! {problem}", file=sys.stderr)
    store.close()
    return 0


def _num(value, digits: int = 2) -> str:
    return "n/a" if value is None else f"{float(value):,.{digits}f}"


def _pct(value) -> str:
    return "n/a" if value is None else f"{float(value) * 100:+.1f}%"


# ---------------------------------------------------------------- test-alert


def cmd_test_alert(args: argparse.Namespace) -> int:
    config = _load(args)
    notifiers, problems = build_notifiers(config.notify.channels)
    for problem in problems:
        print(f"  ! {problem}", file=sys.stderr)

    failed = False
    for notifier in notifiers:
        result = notifier.send(
            "Market Radar test alert",
            "If you can read this on your phone, delivery works.\n\n"
            "This is a test, not a signal.",
            level=args.level,
            url=args.dashboard_url or os.environ.get("RADAR_DASHBOARD_URL"),
        )
        status = "ok" if result.get("ok") else f"FAILED: {result.get('error')}"
        print(f"{notifier.name}: {status}")
        failed = failed or not result.get("ok")
    return 1 if failed else 0


# ---------------------------------------------------------------------- size


def cmd_size(args: argparse.Namespace) -> int:
    config = _load(args)
    inst = config.instrument(args.instrument_id)
    if inst is None:
        print(
            f"Unknown instrument '{args.instrument_id}'. Known: "
            + ", ".join(i.id for i in config.instruments),
            file=sys.stderr,
        )
        return 2

    store = Store(args.db)
    market = MarketData(cache=store)
    bars = market.bars(inst, config.engine.history_days)
    if not bars:
        print(f"No data for {inst.signal_symbol}", file=sys.stderr)
        store.close()
        return 1

    result = sizing_for_instrument(config, inst, bars)
    print(f"{inst.name} — measured on {inst.signal_symbol}, traded as {inst.trade_label}\n")
    for key, value in result.items():
        print(f"  {key:<24} {value}")
    print(
        "\nArithmetic from your own risk settings. Not a recommendation to open "
        "this position."
    )
    store.close()
    return 0


# ------------------------------------------------------------------ selftest


def cmd_selftest(args: argparse.Namespace) -> int:
    from .selftest import run_selftest

    return run_selftest(verbose=not args.quiet)


# ---------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radar",
        description="Market Radar — rule-based market monitoring and alerting.",
    )
    parser.add_argument(
        "--config-dir", default=os.environ.get("RADAR_CONFIG_DIR", "config")
    )
    parser.add_argument("--db", default=os.environ.get("RADAR_DB", "state/radar.db"))
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="validate configuration")
    p_check.set_defaults(func=cmd_check)

    p_run = sub.add_parser("run", help="fetch data, evaluate rules, send alerts")
    p_run.add_argument("--dry-run", action="store_true", help="do not send real alerts")
    p_run.add_argument("--force", action="store_true", help="ignore alert cooldowns")
    p_run.add_argument("--out", default="docs/data.json")
    p_run.add_argument("--dashboard-url", default=None)
    p_run.set_defaults(func=cmd_run)

    p_snap = sub.add_parser("snapshot", help="print current readings as a table")
    p_snap.set_defaults(func=cmd_snapshot)

    p_test = sub.add_parser("test-alert", help="send a test notification")
    p_test.add_argument("--level", default="medium")
    p_test.add_argument("--dashboard-url", default=None)
    p_test.set_defaults(func=cmd_test_alert)

    p_size = sub.add_parser("size", help="position size for an instrument")
    p_size.add_argument("instrument_id")
    p_size.set_defaults(func=cmd_size)

    p_self = sub.add_parser("selftest", help="offline end-to-end test")
    p_self.add_argument("--quiet", action="store_true")
    p_self.set_defaults(func=cmd_selftest)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
