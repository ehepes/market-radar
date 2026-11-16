"""Shared HTTP plumbing and the provider interface.

Every provider turns a symbol into a list of :class:`Bar`, oldest first, and
raises :class:`ProviderError` otherwise. Nothing above this layer knows which
API the data came from.
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from ..models import Bar, ProviderError

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 20

# HTTP statuses worth retrying: rate limits and transient server faults.
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def http_get(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 3,
    backoff: float = 2.0,
) -> str:
    """GET a URL and return the body as text, retrying transient failures.

    Raises ProviderError with ``retryable`` set appropriately so callers can
    decide between backing off and falling through to another provider.
    """
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip",
    }
    if headers:
        request_headers.update(headers)

    last_error: Optional[ProviderError] = None
    for attempt in range(retries):
        request = urllib.request.Request(url, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            retryable = exc.code in RETRYABLE_STATUS
            last_error = ProviderError(
                f"HTTP {exc.code} from {_host(url)}", retryable=retryable
            )
            if not retryable:
                raise last_error from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = ProviderError(
                f"network error contacting {_host(url)}: {exc}", retryable=True
            )

        if attempt < retries - 1:
            time.sleep(backoff * (2**attempt))

    raise last_error or ProviderError(f"failed to GET {_host(url)}", retryable=True)


def http_get_json(url: str, **kwargs: Any) -> Any:
    body = http_get(url, **kwargs)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        snippet = body[:160].replace("\n", " ")
        raise ProviderError(
            f"{_host(url)} returned non-JSON content (got: {snippet!r}). "
            "This usually means the endpoint is behind a bot wall.",
            retryable=False,
        ) from exc


def _host(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc
    except Exception:
        return url.split("/")[2] if "//" in url else url


class RateLimiter:
    """Minimum-interval throttle, so free tiers don't bounce us.

    Twelve Data's free plan allows 8 calls/minute; spacing calls is simpler and
    more predictable than reacting to 429s after the fact.
    """

    def __init__(self, calls_per_minute: int) -> None:
        self.min_interval = 60.0 / calls_per_minute if calls_per_minute > 0 else 0.0
        self._last_call = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_call
        if self._last_call and elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()


class Provider:
    """Interface implemented by every data source."""

    name = "base"
    supports: tuple[str, ...] = ()

    def available(self) -> bool:
        """True when this provider is usable (e.g. its API key is present)."""
        return True

    def daily_bars(self, symbol: str, days: int) -> List[Bar]:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<Provider {self.name}>"


def make_bar(
    day: Any,
    open_: Any,
    high: Any,
    low: Any,
    close: Any,
    volume: Any,
) -> Optional[Bar]:
    """Build a Bar from loosely typed API fields, or None if unusable.

    Providers routinely emit nulls for holidays and halted sessions; a bar with
    no close is worthless for indicators, so it is dropped rather than guessed.
    """
    try:
        close_value = float(close)
    except (TypeError, ValueError):
        return None
    if close_value <= 0:
        return None

    def num(value: Any, fallback: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return fallback
        return parsed if parsed > 0 else fallback

    return Bar(
        day=_as_date(day),
        open=num(open_, close_value),
        high=num(high, close_value),
        low=num(low, close_value),
        close=close_value,
        volume=max(0.0, num(volume, 0.0)),
    )


def _as_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).date()
    text = str(value)
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 2].strip(), fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ProviderError(f"unparseable date {value!r}") from exc
