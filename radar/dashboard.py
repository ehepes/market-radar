"""Builds the JSON payload the phone dashboard reads.

Written to ``docs/data.json`` and served by GitHub Pages. The page is static:
all the intelligence happens in CI, so the dashboard loads instantly and works
offline from cache.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .config import Config
from .models import Bar, Signal
from .portfolio import portfolio_totals


def build_payload(
    config: Config,
    snapshots: Sequence[Dict[str, Any]],
    signals: Sequence[Signal],
    positions: Sequence[Dict[str, Any]],
    recent_signals: Sequence[Dict[str, Any]],
    problems: Sequence[str],
    sources: Dict[str, str],
    sparklines: Optional[Dict[str, List[float]]] = None,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    # A Pages site is public on every GitHub plan below Enterprise, so
    # position data is withheld unless the config explicitly opts in.
    publish_positions = config.dashboard.include_positions
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_human": now.strftime("%d %b %Y, %H:%M UTC"),
        "timezone": config.engine.timezone,
        "totals": (
            portfolio_totals(positions, config.risk)
            if publish_positions
            else {"withheld": True, "positions": len(positions)}
        ),
        "positions": list(positions) if publish_positions else [],
        "snapshots": sorted(snapshots, key=lambda s: str(s.get("name", ""))),
        "signals_now": [s.to_dict() for s in signals],
        "signals_recent": list(recent_signals),
        "problems": list(problems),
        "sources": sources,
        "sparklines": sparklines or {},
        "counts": {
            "instruments": len([i for i in config.instruments if i.enabled]),
            "signals_now": len(signals),
            "critical": len([s for s in signals if s.level == "critical"]),
            "high": len([s for s in signals if s.level == "high"]),
        },
    }


def sparkline_data(bars_by_instrument: Dict[str, List[Bar]], points: int = 60) -> Dict[str, List[float]]:
    """Trailing closes per instrument, normalised to 0-100 for drawing."""
    out: Dict[str, List[float]] = {}
    for instrument_id, bars in bars_by_instrument.items():
        closes = [b.close for b in bars][-points:]
        if len(closes) < 2:
            continue
        low, high = min(closes), max(closes)
        span = high - low
        if span <= 0:
            out[instrument_id] = [50.0] * len(closes)
        else:
            out[instrument_id] = [round((c - low) / span * 100.0, 2) for c in closes]
    return out


def write(payload: Dict[str, Any], path: str | Path = "docs/data.json") -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return target
