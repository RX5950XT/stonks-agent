from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from stonks_agent.adapters.market_data.regional.base import (
    RegionalCapability,
    load_regional_mappings,
    unsupported_observation,
)
from stonks_agent.domain.data_quality import ProviderDataState

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)


@pytest.mark.parametrize(
    ("region", "mic", "currency", "timezone"),
    [
        ("us", "XNAS", "USD", "America/New_York"),
        ("hk", "XHKG", "HKD", "Asia/Hong_Kong"),
        ("tw", "XTAI", "TWD", "Asia/Taipei"),
    ],
)
def test_initial_regional_mappings_are_explicit_and_validated(
    region: str,
    mic: str,
    currency: str,
    timezone: str,
) -> None:
    mappings = load_regional_mappings(Path(f"config/instruments/{region}.yaml"))

    assert mappings
    assert mappings[0].exchange_mic == mic
    assert mappings[0].currency == currency
    assert mappings[0].timezone == timezone
    assert mappings[0].provider_symbol("replay", NOW)


def test_unknown_provider_does_not_fallback_to_symbol_suffix_heuristic() -> None:
    mapping = load_regional_mappings(Path("config/instruments/hk.yaml"))[0]

    with pytest.raises(LookupError, match="no provider symbol"):
        mapping.provider_symbol("yahoo", NOW)


def test_unsupported_capability_is_explicit_not_empty_success() -> None:
    result = unsupported_observation(
        capability=RegionalCapability.FUNDAMENTALS,
        observed_at=NOW,
    )

    assert result.state is ProviderDataState.NOT_SUPPORTED
    assert result.data == ()
    assert result.is_usable is False
