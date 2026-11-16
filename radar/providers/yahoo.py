"""Yahoo Finance — opportunistic fallback with global coverage.

Yahoo is the only free source that covers Xetra, Euronext, LSE and crypto in
one place, but it is unreliable from datacentre IPs: it requires a cookie plus
a 'crumb' token and aggressively rate-limits. Testing from both a residential
Mac and CI returned HTTP 429, so this provider is deliberately a *fallback*
and the engine never depends on it. When it works, it is a free bonus.
"""

from __future__ import annotations

import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional

from ..models import Bar, ProviderError
from .base import DEFAULT_TIMEOUT, USER_AGENT, Provider, make_bar

CHART_ROOT = "https://query1.finance.yahoo.com/v8/finance/chart"
CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
COOKIE_URL = "https://fc.yahoo.com"


class YahooProvider(Provider):
    name = "yahoo"
    supports = ("equity", "etf", "crypto")

    def __init__(self) -> None:
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )
        self._crumb: Optional[str] = None
        self._crumb_failed = False

    def available(self) -> bool:
        # No key needed; whether it actually answers is discovered per call.
        return not self._crumb_failed

    def _fetch(self, url: str) -> str:
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            with self._opener.open(request, timeout=DEFAULT_TIMEOUT) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise ProviderError(
                f"Yahoo returned HTTP {exc.code}", retryable=exc.code in (429, 503)
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"Yahoo unreachable: {exc}", retryable=True) from exc

    def _ensure_crumb(self) -> None:
        if self._crumb:
            return
        try:
            self._fetch(COOKIE_URL)
        except ProviderError:
            # The cookie endpoint 404s by design; we only want its Set-Cookie.
            pass
        crumb = self._fetch(CRUMB_URL).strip()
        if not crumb or " " in crumb or len(crumb) > 32:
            # A rate-limited Yahoo answers this endpoint with prose
            # ("Too Many Requests") rather than an HTTP error code.
            self._crumb_failed = True
            raise ProviderError(
                f"Yahoo would not issue a crumb (said: {crumb[:40]!r}); "
                "it is rate-limiting this IP.",
                retryable=True,
            )
        self._crumb = crumb

    def daily_bars(self, symbol: str, days: int) -> List[Bar]:
        self._ensure_crumb()
        query = urllib.parse.urlencode(
            {
                "range": _range_for(days),
                "interval": "1d",
                "includePrePost": "false",
                "crumb": self._crumb or "",
            }
        )
        raw = self._fetch(f"{CHART_ROOT}/{urllib.parse.quote(symbol)}?{query}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError("Yahoo returned non-JSON content", retryable=True) from exc

        chart = payload.get("chart") or {}
        if chart.get("error"):
            raise ProviderError(
                f"Yahoo error for '{symbol}': {chart['error']}", retryable=False
            )
        results = chart.get("result") or []
        if not results:
            raise ProviderError(f"Yahoo returned no result for '{symbol}'")

        result = results[0]
        stamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]

        bars: List[Bar] = []
        for i, stamp in enumerate(stamps):
            bar = make_bar(
                stamp,
                _at(quote.get("open"), i),
                _at(quote.get("high"), i),
                _at(quote.get("low"), i),
                _at(quote.get("close"), i),
                _at(quote.get("volume"), i),
            )
            if bar:
                bars.append(bar)
        if not bars:
            raise ProviderError(f"Yahoo returned only empty bars for '{symbol}'")
        bars.sort(key=lambda b: b.day)
        return bars


def _at(seq: Optional[list], index: int):
    if not seq or index >= len(seq):
        return None
    return seq[index]


def _range_for(days: int) -> str:
    for threshold, label in ((5, "5d"), (30, "1mo"), (90, "3mo"), (180, "6mo"), (365, "1y")):
        if days <= threshold:
            return label
    return "2y" if days <= 730 else "5y"
