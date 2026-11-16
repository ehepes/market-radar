"""Provider registry and the fallback chain the engine actually calls."""

from __future__ import annotations

from typing import Dict, List, Optional, Protocol, Sequence

from ..models import Bar, Instrument, ProviderError
from .alphavantage import AlphaVantageProvider
from .base import Provider
from .crypto import CoinbaseProvider, KrakenProvider
from .twelvedata import TwelveDataProvider
from .yahoo import YahooProvider

# Order matters: first available provider that answers wins. Crypto leads with
# keyless public exchanges; equities lead with the keyed provider because the
# keyless ones (Yahoo, Stooq) are rate-limited or bot-walled in practice.
CHAINS: Dict[str, Sequence[str]] = {
    "crypto": ("kraken", "coinbase", "yahoo"),
    "equity": ("twelvedata", "yahoo", "alphavantage"),
    "etf": ("twelvedata", "yahoo", "alphavantage"),
}


class BarCache(Protocol):
    """Minimal cache contract; implemented by radar.state.Store."""

    def get_bars(self, symbol: str) -> List[Bar]: ...
    def put_bars(self, symbol: str, bars: Sequence[Bar]) -> None: ...


def build_providers() -> Dict[str, Provider]:
    return {
        "twelvedata": TwelveDataProvider(),
        "kraken": KrakenProvider(),
        "coinbase": CoinbaseProvider(),
        "yahoo": YahooProvider(),
        "alphavantage": AlphaVantageProvider(),
    }


class MarketData:
    """Fetches bars for an instrument, with fallback and a durable cache.

    The cache is not just a speed optimisation: if every provider is down or
    rate-limited, a run still produces signals from the last good data rather
    than silently reporting nothing. Stale data is flagged, never hidden.
    """

    def __init__(
        self,
        cache: Optional[BarCache] = None,
        providers: Optional[Dict[str, Provider]] = None,
    ) -> None:
        self.providers = providers if providers is not None else build_providers()
        self.cache = cache
        self.errors: Dict[str, List[str]] = {}
        self.sources: Dict[str, str] = {}

    def chain_for(self, inst: Instrument) -> List[str]:
        chain = list(CHAINS.get(inst.asset_class, CHAINS["equity"]))
        preferred = (inst.signal_provider or "auto").lower()
        if preferred not in ("auto", "", None) and preferred in self.providers:
            chain = [preferred] + [c for c in chain if c != preferred]
        return chain

    def bars(self, inst: Instrument, days: int) -> List[Bar]:
        """Return daily bars oldest-first, or [] if nothing could be sourced."""
        failures: List[str] = []

        for provider_name in self.chain_for(inst):
            provider = self.providers.get(provider_name)
            if provider is None:
                continue
            if not provider.available():
                failures.append(f"{provider_name}: unavailable (no API key?)")
                continue
            try:
                bars = provider.daily_bars(inst.signal_symbol, days)
            except ProviderError as exc:
                failures.append(f"{provider_name}: {exc}")
                continue
            except Exception as exc:  # defensive: never let one symbol kill a run
                failures.append(f"{provider_name}: unexpected {type(exc).__name__}: {exc}")
                continue

            if bars:
                self.sources[inst.id] = provider_name
                if self.cache:
                    self.cache.put_bars(inst.signal_symbol, bars)
                if failures:
                    self.errors[inst.id] = failures
                return bars
            failures.append(f"{provider_name}: returned no bars")

        # Everything failed — fall back to whatever we last stored.
        if self.cache:
            cached = self.cache.get_bars(inst.signal_symbol)
            if cached:
                self.sources[inst.id] = "cache"
                failures.append(f"using cached data from {cached[-1].day.isoformat()}")
                self.errors[inst.id] = failures
                return cached

        self.errors[inst.id] = failures
        return []


__all__ = [
    "MarketData",
    "Provider",
    "build_providers",
    "CHAINS",
    "TwelveDataProvider",
    "KrakenProvider",
    "CoinbaseProvider",
    "YahooProvider",
    "AlphaVantageProvider",
]
