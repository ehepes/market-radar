"""Alpha Vantage — optional, for genuinely EU-listed quotes.

The free tier allows only ~25 requests/day, which is useless for a whole
watchlist but perfectly adequate for spot-checking a handful of EU-listed
instruments (the UCITS ETF you actually buy) against the US proxy the engine
measures. Entirely optional: without a key the engine ignores it.

Symbol suffixes: .LON (London), .DEX (Xetra), .PAR (Paris), .AMS (Amsterdam),
.MIL (Milan), .MAD (Madrid).
"""

from __future__ import annotations

import os
import urllib.parse
from typing import List

from ..models import Bar, ProviderError
from .base import Provider, RateLimiter, http_get_json, make_bar

API_ROOT = "https://www.alphavantage.co/query"


class AlphaVantageProvider(Provider):
    name = "alphavantage"
    supports = ("equity", "etf")

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("ALPHAVANTAGE_API_KEY", "")
        # Free tier is ~5 calls/minute; stay well inside it.
        self.limiter = RateLimiter(4)

    def available(self) -> bool:
        return bool(self.api_key)

    def daily_bars(self, symbol: str, days: int) -> List[Bar]:
        if not self.available():
            raise ProviderError("ALPHAVANTAGE_API_KEY is not set", retryable=False)

        query = urllib.parse.urlencode(
            {
                "function": "TIME_SERIES_DAILY",
                "symbol": symbol,
                "outputsize": "full" if days > 100 else "compact",
                "apikey": self.api_key,
            }
        )
        self.limiter.wait()
        payload = http_get_json(f"{API_ROOT}?{query}")

        # Alpha Vantage signals quota exhaustion with a 200 and a prose field.
        for field in ("Note", "Information", "Error Message"):
            if field in payload:
                raise ProviderError(
                    f"Alpha Vantage: {payload[field]}", retryable=(field == "Note")
                )

        series = payload.get("Time Series (Daily)")
        if not series:
            raise ProviderError(f"Alpha Vantage returned no series for '{symbol}'")

        bars: List[Bar] = []
        for day, row in series.items():
            bar = make_bar(
                day,
                row.get("1. open"),
                row.get("2. high"),
                row.get("3. low"),
                row.get("4. close"),
                row.get("5. volume"),
            )
            if bar:
                bars.append(bar)
        bars.sort(key=lambda b: b.day)
        return bars[-days:] if days < len(bars) else bars
