"""Twelve Data — primary source for US equities and ETFs.

Free "Basic" plan: 800 requests/day, 8 requests/minute, covering US equities,
US ETFs, forex and crypto. European venues (Xetra, Euronext, LSE) are NOT on
the free plan, which is precisely why watchlist entries separate the symbol we
measure from the instrument you buy. See config/watchlist.toml.
"""

from __future__ import annotations

import os
import urllib.parse
from typing import List

from ..models import Bar, ProviderError
from .base import Provider, RateLimiter, http_get_json, make_bar

API_ROOT = "https://api.twelvedata.com"
FREE_CALLS_PER_MINUTE = 8


class TwelveDataProvider(Provider):
    name = "twelvedata"
    supports = ("equity", "etf", "crypto", "forex")

    def __init__(self, api_key: str | None = None, calls_per_minute: int | None = None) -> None:
        self.api_key = api_key or os.environ.get("TWELVEDATA_API_KEY", "")
        self.limiter = RateLimiter(calls_per_minute or FREE_CALLS_PER_MINUTE)

    def available(self) -> bool:
        return bool(self.api_key)

    def daily_bars(self, symbol: str, days: int) -> List[Bar]:
        if not self.available():
            raise ProviderError(
                "TWELVEDATA_API_KEY is not set. Get a free key at "
                "https://twelvedata.com/pricing and add it as a repository secret.",
                retryable=False,
            )

        query = urllib.parse.urlencode(
            {
                "symbol": symbol,
                "interval": "1day",
                # Ask for a few extra bars; holidays thin out the series.
                "outputsize": min(max(days, 30), 5000),
                "order": "ASC",
                "apikey": self.api_key,
            }
        )
        self.limiter.wait()
        payload = http_get_json(f"{API_ROOT}/time_series?{query}")

        if isinstance(payload, dict) and payload.get("status") == "error":
            code = payload.get("code")
            message = str(payload.get("message", "unknown error"))
            # 429 = out of credits for the minute/day; worth retrying later.
            raise ProviderError(
                f"Twelve Data rejected '{symbol}': {message}",
                retryable=code in (429, 500, 503),
            )

        values = payload.get("values") if isinstance(payload, dict) else None
        if not values:
            raise ProviderError(
                f"Twelve Data returned no bars for '{symbol}'", retryable=False
            )

        bars: List[Bar] = []
        for row in values:
            bar = make_bar(
                row.get("datetime"),
                row.get("open"),
                row.get("high"),
                row.get("low"),
                row.get("close"),
                row.get("volume"),
            )
            if bar:
                bars.append(bar)

        # The API honours order=ASC, but never trust ordering you depend on.
        bars.sort(key=lambda b: b.day)
        return bars
