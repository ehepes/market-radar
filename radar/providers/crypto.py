"""Crypto providers: Kraken primary, Coinbase fallback.

Both are public, keyless and quote directly in EUR, which matches how Revolut
Ireland prices crypto. Verified working against live endpoints.
"""

from __future__ import annotations

import urllib.parse
from typing import Dict, List

from ..models import Bar, ProviderError
from .base import Provider, http_get_json, make_bar

DAY_SECONDS = 86400

# Kraken uses its own asset codes (XBT for bitcoin) and returns result keys in
# a normalised form (XXBTZEUR), so symbols are mapped explicitly.
KRAKEN_PAIRS: Dict[str, str] = {
    "BTC-EUR": "XBTEUR",
    "ETH-EUR": "ETHEUR",
    "SOL-EUR": "SOLEUR",
    "ADA-EUR": "ADAEUR",
    "XRP-EUR": "XRPEUR",
    "DOT-EUR": "DOTEUR",
    "LINK-EUR": "LINKEUR",
    "AVAX-EUR": "AVAXEUR",
    "LTC-EUR": "LTCEUR",
    "BTC-USD": "XBTUSD",
    "ETH-USD": "ETHUSD",
}


class KrakenProvider(Provider):
    name = "kraken"
    supports = ("crypto",)

    def daily_bars(self, symbol: str, days: int) -> List[Bar]:
        pair = KRAKEN_PAIRS.get(symbol.upper())
        if not pair:
            raise ProviderError(
                f"No Kraken pair mapped for '{symbol}'. Add it to KRAKEN_PAIRS "
                "in radar/providers/crypto.py.",
                retryable=False,
            )

        query = urllib.parse.urlencode({"pair": pair, "interval": 1440})
        payload = http_get_json(f"https://api.kraken.com/0/public/OHLC?{query}")

        errors = payload.get("error") or []
        if errors:
            raise ProviderError(f"Kraken error for '{symbol}': {errors}", retryable=True)

        result = payload.get("result", {})
        series = next((v for k, v in result.items() if k != "last"), None)
        if not series:
            raise ProviderError(f"Kraken returned no candles for '{symbol}'")

        bars: List[Bar] = []
        for row in series:
            # [time, open, high, low, close, vwap, volume, count]
            bar = make_bar(row[0], row[1], row[2], row[3], row[4], row[6])
            if bar:
                bars.append(bar)
        bars.sort(key=lambda b: b.day)
        return bars[-days:] if days < len(bars) else bars


class CoinbaseProvider(Provider):
    name = "coinbase"
    supports = ("crypto",)

    def daily_bars(self, symbol: str, days: int) -> List[Bar]:
        product = symbol.upper()
        query = urllib.parse.urlencode({"granularity": DAY_SECONDS})
        payload = http_get_json(
            f"https://api.exchange.coinbase.com/products/{product}/candles?{query}"
        )
        if not isinstance(payload, list) or not payload:
            raise ProviderError(f"Coinbase returned no candles for '{symbol}'")

        bars: List[Bar] = []
        for row in payload:
            # Coinbase column order is [time, low, high, open, close, volume]
            bar = make_bar(row[0], row[3], row[2], row[1], row[4], row[5])
            if bar:
                bars.append(bar)
        bars.sort(key=lambda b: b.day)
        return bars[-days:] if days < len(bars) else bars
