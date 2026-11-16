"""Earnings calendar — the thing price data cannot tell you.

The rest of the engine reads prices, so it can say "Nvidia fell 12.9% over
five sessions" but not "Nvidia reports tonight". The second is usually the
more useful fact, and the whole point of a watch-this-today alert.

Primary source is Nasdaq's public calendar: keyless, verified working, and it
reports whether a company announces before the open or after the close. One
request per trading day in the window, so a five-day look-ahead costs five
requests and no API quota at all.

Finnhub is an optional fallback when FINNHUB_API_KEY is set. If every source
fails, the calendar is empty, earnings rules stay quiet, and the run reports
the problem rather than pretending there are no events.
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from ..models import ProviderError
from .base import USER_AGENT, http_get_json

NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings"
FINNHUB_ROOT = "https://finnhub.io/api/v1"

# Nasdaq's own vocabulary for when a company reports.
WHEN_TEXT = {
    "time-pre-market": "before the US open",
    "time-after-hours": "after the US close",
    "time-not-supplied": "at an unconfirmed time",
    "bmo": "before the US open",
    "amc": "after the US close",
    "dmh": "during US market hours",
}


@dataclass(frozen=True)
class EarningsEvent:
    """One scheduled report."""

    symbol: str
    day: date
    when: str = ""
    eps_forecast: Optional[str] = None

    @property
    def when_text(self) -> str:
        return WHEN_TEXT.get(self.when.lower(), "at an unconfirmed time")

    def days_away(self, today: Optional[date] = None) -> int:
        return (self.day - (today or datetime.now(timezone.utc).date())).days

    def describe(self, today: Optional[date] = None) -> str:
        """Human phrasing an alert can drop straight into a sentence."""
        days = self.days_away(today)
        if days < 0:
            return f"reported {abs(days)} day{'s' if abs(days) != 1 else ''} ago"
        if days == 0:
            return f"reports TODAY {self.when_text}"
        if days == 1:
            return f"reports TOMORROW {self.when_text}"
        return f"reports in {days} days ({self.day.isoformat()})"


class EarningsCalendar:
    """Fetches the calendar once per run and serves it to every rule."""

    name = "earnings"

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.finnhub_key = api_key or os.environ.get("FINNHUB_API_KEY", "")
        self._cache: Optional[Dict[str, List[EarningsEvent]]] = None
        self.problems: List[str] = []

    def fetch(self, days_ahead: int = 7) -> Dict[str, List[EarningsEvent]]:
        if self._cache is not None:
            return self._cache

        by_symbol: Dict[str, List[EarningsEvent]] = {}
        today = datetime.now(timezone.utc).date()

        for offset in range(0, days_ahead + 1):
            day = today + timedelta(days=offset)
            if day.weekday() >= 5:      # companies do not report at weekends
                continue
            try:
                for event in self._nasdaq_day(day):
                    by_symbol.setdefault(event.symbol, []).append(event)
            except ProviderError as exc:
                self.problems.append(f"earnings calendar for {day}: {exc}")

        if not by_symbol and self.finnhub_key:
            try:
                by_symbol = self._finnhub(days_ahead)
            except ProviderError as exc:
                self.problems.append(f"finnhub earnings fallback: {exc}")

        for events in by_symbol.values():
            events.sort(key=lambda e: e.day)
        self._cache = by_symbol
        return by_symbol

    def _nasdaq_day(self, day: date) -> List[EarningsEvent]:
        query = urllib.parse.urlencode({"date": day.isoformat()})
        payload = http_get_json(
            f"{NASDAQ_URL}?{query}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            retries=2,
        )
        rows = ((payload or {}).get("data") or {}).get("rows") or []
        out: List[EarningsEvent] = []
        for row in rows:
            symbol = str(row.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            out.append(
                EarningsEvent(
                    symbol=symbol,
                    day=day,
                    when=str(row.get("time") or ""),
                    eps_forecast=_clean(row.get("epsForecast")),
                )
            )
        return out

    def _finnhub(self, days_ahead: int) -> Dict[str, List[EarningsEvent]]:
        today = datetime.now(timezone.utc).date()
        query = urllib.parse.urlencode(
            {
                "from": today.isoformat(),
                "to": (today + timedelta(days=days_ahead)).isoformat(),
                "token": self.finnhub_key,
            }
        )
        payload = http_get_json(f"{FINNHUB_ROOT}/calendar/earnings?{query}")
        out: Dict[str, List[EarningsEvent]] = {}
        for row in (payload or {}).get("earningsCalendar", []) or []:
            symbol = str(row.get("symbol", "")).upper()
            try:
                day = datetime.strptime(str(row.get("date")), "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if not symbol:
                continue
            out.setdefault(symbol, []).append(
                EarningsEvent(symbol=symbol, day=day, when=str(row.get("hour") or ""))
            )
        return out

    def for_symbol(self, symbol: str) -> List[EarningsEvent]:
        return self.fetch().get(symbol.upper(), [])

    def next_event(self, symbol: str) -> Optional[EarningsEvent]:
        """The soonest event that has not already happened."""
        today = datetime.now(timezone.utc).date()
        for event in self.for_symbol(symbol):
            if event.day >= today:
                return event
        return None


def _clean(value) -> Optional[str]:
    text = str(value or "").strip()
    return text or None
