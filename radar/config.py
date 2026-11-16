"""Configuration loading and validation.

Config lives in TOML so it stays comfortable to hand-edit. Every value the
engine acts on is validated here rather than at the point of use, so a typo in
a config file produces one clear error instead of a crash mid-run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import Holding, Instrument, Rule

try:
    import tomllib
except ModuleNotFoundError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "Market Radar needs Python 3.11 or newer (for the built-in tomllib "
        f"TOML reader). You are running an older interpreter: {exc}"
    ) from exc

CONFIG_DIR = Path(os.environ.get("RADAR_CONFIG_DIR", "config"))

VALID_ASSET_CLASSES = {"equity", "etf", "crypto"}
VALID_LEVELS = {"info", "low", "medium", "high", "critical"}


class ConfigError(RuntimeError):
    """A config file is malformed or internally inconsistent."""


@dataclass
class EngineSettings:
    timezone: str = "Europe/Dublin"
    history_days: int = 400
    min_bars: int = 60
    crypto_quote_currency: str = "EUR"


@dataclass
class NotifySettings:
    channels: List[str] = field(default_factory=lambda: ["console"])
    min_level: str = "low"
    max_alerts_per_run: int = 12
    cooldown_hours: int = 48
    quiet_start: Optional[str] = None
    quiet_end: Optional[str] = None
    always_send_levels: List[str] = field(default_factory=lambda: ["critical"])


@dataclass
class DashboardSettings:
    """What the published dashboard is allowed to show.

    A GitHub Pages site is public on every plan below Enterprise, so position
    sizes, entry prices and P/L are withheld by default. Stop and target
    alerts still reach your phone — they just are not published to the web.
    """

    include_positions: bool = False


@dataclass
class RiskSettings:
    """Inputs for position-size arithmetic. Your numbers, not suggestions."""

    account_size: float = 0.0
    account_currency: str = "EUR"
    risk_per_trade_pct: float = 1.0
    max_position_pct: float = 20.0


@dataclass
class Config:
    engine: EngineSettings
    notify: NotifySettings
    risk: RiskSettings
    dashboard: DashboardSettings
    instruments: List[Instrument]
    rules: Dict[str, Rule]
    holdings: List[Holding]

    def instrument(self, instrument_id: str) -> Optional[Instrument]:
        for inst in self.instruments:
            if inst.id == instrument_id:
                return inst
        return None

    def rules_for(self, inst: Instrument) -> List[Rule]:
        """Resolve an instrument's rule ids, skipping any that don't exist."""
        return [self.rules[r] for r in inst.rules if r in self.rules]

    def unknown_rule_refs(self) -> List[str]:
        missing = []
        for inst in self.instruments:
            for ref in inst.rules:
                if ref not in self.rules:
                    missing.append(f"{inst.id} -> {ref}")
        return missing


def _read_toml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


def _require(table: Dict[str, Any], key: str, where: str) -> Any:
    if key not in table or table[key] in (None, ""):
        raise ConfigError(f"{where} is missing required field '{key}'")
    return table[key]


def load_instruments(path: Path) -> List[Instrument]:
    raw = _read_toml(path)
    entries = raw.get("instrument", [])
    if not entries:
        raise ConfigError(f"{path} defines no [[instrument]] entries")

    instruments: List[Instrument] = []
    seen: set[str] = set()
    for idx, entry in enumerate(entries):
        where = f"{path} [[instrument]] #{idx + 1}"
        inst_id = str(_require(entry, "id", where))
        if inst_id in seen:
            raise ConfigError(f"{where} reuses id '{inst_id}'")
        seen.add(inst_id)

        asset_class = str(entry.get("asset_class", "equity")).lower()
        if asset_class not in VALID_ASSET_CLASSES:
            raise ConfigError(
                f"{where} has asset_class '{asset_class}'; "
                f"expected one of {sorted(VALID_ASSET_CLASSES)}"
            )

        instruments.append(
            Instrument(
                id=inst_id,
                name=str(entry.get("name", inst_id)),
                asset_class=asset_class,
                signal_symbol=str(_require(entry, "signal_symbol", where)),
                signal_provider=str(entry.get("signal_provider", "auto")),
                rules=[str(r) for r in entry.get("rules", [])],
                trade_ticker=_opt_str(entry.get("trade_ticker")),
                trade_isin=_opt_str(entry.get("trade_isin")),
                trade_venue=_opt_str(entry.get("trade_venue")),
                trade_currency=_opt_str(entry.get("trade_currency")),
                trade_note=_opt_str(entry.get("trade_note")),
                enabled=bool(entry.get("enabled", True)),
            )
        )
    return instruments


def load_rules(path: Path) -> Dict[str, Rule]:
    raw = _read_toml(path)
    entries = raw.get("rule", [])
    if not entries:
        raise ConfigError(f"{path} defines no [[rule]] entries")

    rules: Dict[str, Rule] = {}
    for idx, entry in enumerate(entries):
        where = f"{path} [[rule]] #{idx + 1}"
        rule_id = str(_require(entry, "id", where))
        if rule_id in rules:
            raise ConfigError(f"{where} reuses id '{rule_id}'")

        level = str(entry.get("level", "medium")).lower()
        if level not in VALID_LEVELS:
            raise ConfigError(
                f"{where} has level '{level}'; expected one of {sorted(VALID_LEVELS)}"
            )

        params = {
            k: v
            for k, v in entry.items()
            if k not in {"id", "kind", "label", "level"}
        }
        rules[rule_id] = Rule(
            id=rule_id,
            kind=str(_require(entry, "kind", where)),
            label=str(entry.get("label", rule_id)),
            level=level,
            params=params,
        )
    return rules


def load_portfolio(path: Path) -> List[Holding]:
    """Load holdings from the file, or from the RADAR_PORTFOLIO_TOML env var.

    The env var wins when set. That exists so your positions can live in an
    encrypted repository secret rather than in a public repo, while the
    watchlist (which is not sensitive) stays in version control.
    """
    inline = os.environ.get("RADAR_PORTFOLIO_TOML", "").strip()
    if inline:
        try:
            raw = tomllib.loads(inline)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(
                f"RADAR_PORTFOLIO_TOML is not valid TOML: {exc}"
            ) from exc
    else:
        raw = _read_toml(path)
    holdings: List[Holding] = []
    for idx, entry in enumerate(raw.get("holding", [])):
        where = f"{path} [[holding]] #{idx + 1}"
        holdings.append(
            Holding(
                instrument_id=str(_require(entry, "instrument_id", where)),
                quantity=float(_require(entry, "quantity", where)),
                entry_price=float(_require(entry, "entry_price", where)),
                entry_currency=str(entry.get("entry_currency", "EUR")),
                stop=_opt_float(entry.get("stop")),
                target=_opt_float(entry.get("target")),
                trailing_stop_pct=_opt_float(entry.get("trailing_stop_pct")),
                opened=_opt_str(entry.get("opened")),
                note=_opt_str(entry.get("note")),
            )
        )
    return holdings


def load_config(config_dir: Optional[Path] = None) -> Config:
    base = Path(config_dir) if config_dir else CONFIG_DIR
    if not base.exists():
        raise ConfigError(f"Config directory '{base}' does not exist")

    settings = _read_toml(base / "radar.toml")
    engine_raw = settings.get("engine", {})
    notify_raw = settings.get("notify", {})
    risk_raw = settings.get("risk", {})

    engine = EngineSettings(
        timezone=str(engine_raw.get("timezone", "Europe/Dublin")),
        history_days=int(engine_raw.get("history_days", 400)),
        min_bars=int(engine_raw.get("min_bars", 60)),
        crypto_quote_currency=str(engine_raw.get("crypto_quote_currency", "EUR")),
    )

    channels = notify_raw.get("channels", ["console"])
    if isinstance(channels, str):
        channels = [channels]
    notify = NotifySettings(
        channels=[str(c).lower() for c in channels],
        min_level=str(notify_raw.get("min_level", "low")).lower(),
        max_alerts_per_run=int(notify_raw.get("max_alerts_per_run", 12)),
        cooldown_hours=int(notify_raw.get("cooldown_hours", 48)),
        quiet_start=_opt_str(notify_raw.get("quiet_start")),
        quiet_end=_opt_str(notify_raw.get("quiet_end")),
        always_send_levels=[
            str(x).lower() for x in notify_raw.get("always_send_levels", ["critical"])
        ],
    )
    if notify.min_level not in VALID_LEVELS:
        raise ConfigError(
            f"notify.min_level '{notify.min_level}' is not one of {sorted(VALID_LEVELS)}"
        )

    dashboard = DashboardSettings(
        include_positions=bool(
            settings.get("dashboard", {}).get("include_positions", False)
        )
    )

    risk = RiskSettings(
        account_size=float(risk_raw.get("account_size", 0.0)),
        account_currency=str(risk_raw.get("account_currency", "EUR")),
        risk_per_trade_pct=float(risk_raw.get("risk_per_trade_pct", 1.0)),
        max_position_pct=float(risk_raw.get("max_position_pct", 20.0)),
    )

    config = Config(
        engine=engine,
        notify=notify,
        risk=risk,
        dashboard=dashboard,
        instruments=load_instruments(base / "watchlist.toml"),
        rules=load_rules(base / "rules.toml"),
        holdings=load_portfolio(base / "portfolio.toml"),
    )

    missing = config.unknown_rule_refs()
    if missing:
        raise ConfigError(
            "Watchlist references rules that are not defined in rules.toml: "
            + ", ".join(missing)
        )

    known_ids = {i.id for i in config.instruments}
    for holding in config.holdings:
        if holding.instrument_id not in known_ids:
            raise ConfigError(
                f"portfolio.toml holding references unknown instrument "
                f"'{holding.instrument_id}'"
            )
    return config


def _opt_str(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return str(value)


def _opt_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)
