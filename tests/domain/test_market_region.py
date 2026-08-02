from __future__ import annotations

import re
from pathlib import Path

import pytest

from stonks_agent.domain.market_region import (
    MARKET_EXCHANGE_TIMEZONES,
    MARKET_MICS,
    exchange_timezone_for_market,
    market_for_symbol,
)

SIDECAR_SURFACE = Path("sidecars/openbb/surface.py")


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("AAPL", "US"),
        ("BRK.B", "US"),
        ("2330.TW", "TW"),
        ("2330.tw", "TW"),
        ("6488.TWO", "TW"),
        ("0700.HK", "HK"),
    ],
)
def test_market_is_resolved_from_the_provider_symbol_suffix(
    symbol: str,
    expected: str,
) -> None:
    assert market_for_symbol(symbol) == expected


def test_every_known_market_has_a_mic_and_an_exchange_timezone() -> None:
    assert set(MARKET_MICS) == set(MARKET_EXCHANGE_TIMEZONES)
    for market in MARKET_MICS:
        assert exchange_timezone_for_market(market) is not None


def test_unknown_market_has_no_exchange_timezone() -> None:
    assert exchange_timezone_for_market("XX") is None


def test_sidecar_mirrors_the_core_symbol_suffix_map() -> None:
    """The isolated AGPL sidecar cannot import core, so the maps must agree."""

    source = SIDECAR_SURFACE.read_text(encoding="utf-8")
    match = re.search(r"_SUFFIX_MARKETS: Final = \((.*?)\)\n", source, re.DOTALL)
    assert match is not None
    mirrored = dict(re.findall(r'\("(\.[A-Z]+)", "([A-Z]{2})"\)', match.group(1)))

    assert mirrored == {".TW": "TW", ".TWO": "TW", ".HK": "HK"}
    for symbol, expected in (("2330.TW", "TW"), ("0700.HK", "HK")):
        assert mirrored[symbol[symbol.index(".") :]] == expected
