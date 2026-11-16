"""SQLite-backed state: bar cache, alert de-duplication, signal history.

Two jobs that matter in production:

1. *De-duplication.* A 50/200 crossover stays true for weeks. Without a
   cooldown the engine would alert every single run and you would mute it
   within a week, which defeats the whole point.
2. *Durability.* A cached bar series means a provider outage degrades the run
   instead of blanking it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .models import Bar, Signal

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    day    TEXT NOT NULL,
    open   REAL NOT NULL,
    high   REAL NOT NULL,
    low    REAL NOT NULL,
    close  REAL NOT NULL,
    volume REAL NOT NULL,
    PRIMARY KEY (symbol, day)
);

CREATE TABLE IF NOT EXISTS alerts (
    dedupe_key   TEXT PRIMARY KEY,
    last_sent_at TEXT NOT NULL,
    level        TEXT NOT NULL,
    send_count   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS signal_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at      TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    rule_id       TEXT NOT NULL,
    level         TEXT NOT NULL,
    headline      TEXT NOT NULL,
    detail        TEXT NOT NULL,
    evidence      TEXT NOT NULL DEFAULT '{}',
    notified      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_signal_log_fired ON signal_log (fired_at DESC);

CREATE TABLE IF NOT EXISTS runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at    TEXT NOT NULL,
    summary   TEXT NOT NULL DEFAULT '{}'
);
"""


class Store:
    def __init__(self, path: str | Path = "state/radar.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- bars

    def put_bars(self, symbol: str, bars: Sequence[Bar]) -> None:
        rows = [
            (symbol, b.day.isoformat(), b.open, b.high, b.low, b.close, b.volume)
            for b in bars
        ]
        with closing(self.conn.cursor()) as cur:
            cur.executemany(
                "INSERT INTO bars (symbol, day, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(symbol, day) DO UPDATE SET "
                "open=excluded.open, high=excluded.high, low=excluded.low, "
                "close=excluded.close, volume=excluded.volume",
                rows,
            )
        self.conn.commit()

    def get_bars(self, symbol: str, limit: Optional[int] = None) -> List[Bar]:
        sql = "SELECT * FROM bars WHERE symbol = ? ORDER BY day ASC"
        rows = self.conn.execute(sql, (symbol,)).fetchall()
        bars = [
            Bar(
                day=date.fromisoformat(r["day"]),
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
            )
            for r in rows
        ]
        return bars[-limit:] if limit and limit < len(bars) else bars

    def prune_bars(self, keep_days: int = 800) -> int:
        """Drop ancient bars so the committed DB stays small."""
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=keep_days)).isoformat()
        cur = self.conn.execute("DELETE FROM bars WHERE day < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    # -------------------------------------------------------------- alerts

    def should_send(self, signal: Signal, cooldown_hours: int) -> bool:
        """False when this exact signal was already sent inside the cooldown.

        An escalation in severity always gets through — if a 'watch this' turns
        into 'your stop is breached', you want to hear about it.
        """
        row = self.conn.execute(
            "SELECT last_sent_at, level FROM alerts WHERE dedupe_key = ?",
            (signal.dedupe_key,),
        ).fetchone()
        if row is None:
            return True

        from .models import LEVELS

        previous_rank = LEVELS.index(row["level"]) if row["level"] in LEVELS else 0
        if signal.level_rank > previous_rank:
            return True

        try:
            last_sent = datetime.fromisoformat(row["last_sent_at"])
        except ValueError:
            return True
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - last_sent
        return age >= timedelta(hours=cooldown_hours)

    def mark_sent(self, signal: Signal) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO alerts (dedupe_key, last_sent_at, level, send_count) "
            "VALUES (?, ?, ?, 1) "
            "ON CONFLICT(dedupe_key) DO UPDATE SET "
            "last_sent_at = excluded.last_sent_at, level = excluded.level, "
            "send_count = alerts.send_count + 1",
            (signal.dedupe_key, now, signal.level),
        )
        self.conn.commit()

    def clear_cooldowns(self) -> int:
        cur = self.conn.execute("DELETE FROM alerts")
        self.conn.commit()
        return cur.rowcount

    # -------------------------------------------------------------- history

    def record_signal(self, signal: Signal, notified: bool) -> None:
        self.conn.execute(
            "INSERT INTO signal_log "
            "(fired_at, instrument_id, rule_id, level, headline, detail, evidence, notified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                signal.fired_at,
                signal.instrument_id,
                signal.rule_id,
                signal.level,
                signal.headline,
                signal.detail,
                json.dumps(signal.evidence, default=str),
                1 if notified else 0,
            ),
        )
        self.conn.commit()

    def recent_signals(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT fired_at, instrument_id, rule_id, level, headline, detail, "
            "evidence, notified FROM signal_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            try:
                item["evidence"] = json.loads(item["evidence"])
            except (TypeError, json.JSONDecodeError):
                item["evidence"] = {}
            item["notified"] = bool(item["notified"])
            out.append(item)
        return out

    def record_run(self, summary: Dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO runs (ran_at, summary) VALUES (?, ?)",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                json.dumps(summary, default=str),
            ),
        )
        self.conn.commit()

    def last_run_at(self) -> Optional[str]:
        row = self.conn.execute(
            "SELECT ran_at FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["ran_at"] if row else None
