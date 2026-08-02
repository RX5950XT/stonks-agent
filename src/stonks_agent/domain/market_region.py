"""The exact markets this deployment can resolve to an exchange and timezone."""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Literal

type Market = Literal["US", "HK", "TW"]

MARKET_MICS: Final[MappingProxyType[str, str]] = MappingProxyType(
    {"US": "XNAS", "HK": "XHKG", "TW": "XTAI"}
)
# OpenBB serializes yfinance intraday bars as naive exchange-local timestamps,
# so a bar can only become an exact UTC instant through its own exchange zone.
# Reading a 13:30 Asia/Taipei bar as America/New_York yields a future instant
# and the canonical flow rejects it as openbb_future_data.
MARKET_EXCHANGE_TIMEZONES: Final[MappingProxyType[str, str]] = MappingProxyType(
    {"US": "America/New_York", "HK": "Asia/Hong_Kong", "TW": "Asia/Taipei"}
)
# Provider symbol suffixes are the only supported market discriminator. Anything
# else stays US so ordinary dotted US tickers such as BRK.B keep resolving.
_SUFFIX_MARKETS: Final[tuple[tuple[str, str], ...]] = (
    (".TW", "TW"),
    (".TWO", "TW"),
    (".HK", "HK"),
)


def market_for_symbol(symbol: str) -> str:
    """Resolve the market one provider symbol belongs to."""

    upper = symbol.upper()
    for suffix, market in _SUFFIX_MARKETS:
        if upper.endswith(suffix):
            return market
    return "US"


def exchange_timezone_for_market(market: str) -> str | None:
    """Return the IANA exchange zone, or None when the market is unknown."""

    return MARKET_EXCHANGE_TIMEZONES.get(market)
