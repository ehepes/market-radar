"""Core data types shared across the engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

# Severity ladder. Maps onto Pushover priorities in radar/notify/pushover.py.
LEVELS = ("info", "low", "medium", "high", "critical")


@dataclass(frozen=True)
class Bar:
    """One daily OHLCV bar."""

    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Instrument:
    """A thing to watch, plus the thing you would actually buy.

    These are deliberately two different fields. Free market data covers
    US-listed symbols well and EU venues badly, while Revolut Ireland
    (Revolut Securities Europe UAB) sells EU-listed UCITS ETFs and cannot
    sell you US-domiciled ETFs at all. So the engine computes signals on
    ``signal_symbol`` and tells you to trade ``trade_ticker``/``trade_isin``.
    """

    id: str
    name: str
    asset_class: str                     # equity | etf | crypto
    signal_symbol: str
    signal_provider: str = "twelvedata"
    rules: List[str] = field(default_factory=list)

    trade_ticker: Optional[str] = None
    trade_isin: Optional[str] = None
    trade_venue: Optional[str] = None
    trade_currency: Optional[str] = None
    trade_note: Optional[str] = None
    enabled: bool = True

    @property
    def trade_label(self) -> str:
        """How to describe the executable instrument in an alert."""
        if not self.trade_ticker:
            return self.signal_symbol
        bits = [self.trade_ticker]
        if self.trade_venue:
            bits.append(f"on {self.trade_venue}")
        if self.trade_isin:
            bits.append(f"({self.trade_isin})")
        return " ".join(bits)

    @property
    def is_proxy(self) -> bool:
        """True when signals come from a different symbol than you trade."""
        return bool(self.trade_ticker) and self.trade_ticker != self.signal_symbol


@dataclass
class Holding:
    """An open position, with the exit levels *you* chose."""

    instrument_id: str
    quantity: float
    entry_price: float
    entry_currency: str = "EUR"
    stop: Optional[float] = None
    target: Optional[float] = None
    trailing_stop_pct: Optional[float] = None
    opened: Optional[str] = None
    note: Optional[str] = None


@dataclass
class Signal:
    """One fired rule. The unit of everything the engine emits."""

    instrument_id: str
    rule_id: str
    level: str
    headline: str
    detail: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    fired_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    @property
    def dedupe_key(self) -> str:
        """Identity used to suppress the same alert firing day after day."""
        return f"{self.instrument_id}:{self.rule_id}"

    @property
    def level_rank(self) -> int:
        return LEVELS.index(self.level) if self.level in LEVELS else 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "rule_id": self.rule_id,
            "level": self.level,
            "headline": self.headline,
            "detail": self.detail,
            "evidence": self.evidence,
            "fired_at": self.fired_at,
        }


@dataclass
class Rule:
    """A configured rule. ``kind`` selects the evaluator in signals.py."""

    id: str
    kind: str
    label: str = ""
    level: str = "medium"
    params: Dict[str, Any] = field(default_factory=dict)

    def param(self, name: str, default: Any = None) -> Any:
        return self.params.get(name, default)


class ProviderError(RuntimeError):
    """Raised when a data provider cannot serve a symbol.

    Carries ``retryable`` so the orchestrator can tell a rate-limit (back off,
    try the fallback) from a bad symbol (skip it and report it).
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
