from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from stonks_agent.adapters.market_data.financial_datasets import (
    FINANCIAL_DATASETS_SUPPORT,
)
from stonks_agent.adapters.market_data.openbb_rest import OPENBB_REST_SUPPORT
from stonks_agent.adapters.market_data.regional.base import (
    RegionalCapability,
    RegionalProviderCapability,
    load_regional_mappings,
    unsupported_observation,
)
from stonks_agent.domain.data_quality import ProviderDataState
from stonks_agent.domain.provider_policy import load_provider_policies

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)
FIXTURE_MANIFEST = Path("tests/fixtures/market_data/manifest.yaml")
PROVIDER_POLICIES = Path("config/providers/default.yaml")


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


@pytest.mark.parametrize(
    ("region", "expected"),
    [
        (
            "us",
            {
                RegionalCapability.PRICES_DAILY,
                RegionalCapability.PRICES_INTRADAY,
                RegionalCapability.CORPORATE_ACTIONS,
            },
        ),
        ("hk", {RegionalCapability.PRICES_INTRADAY}),
        ("tw", {RegionalCapability.PRICES_DAILY}),
    ],
)
def test_declared_regional_capabilities_have_replay_fixture_evidence(
    region: str,
    expected: set[RegionalCapability],
) -> None:
    mapping = load_regional_mappings(Path(f"config/instruments/{region}.yaml"))[0]

    assert set(mapping.supported_capabilities) == expected
    assert expected == _fixture_capabilities(region.upper())


def test_non_us_policies_only_route_to_adapters_declaring_that_market() -> None:
    """A market may only reach an external adapter that verified that market."""

    policies = load_provider_policies(PROVIDER_POLICIES)
    declared = {
        (capability.market, capability.provider)
        for capability in FINANCIAL_DATASETS_SUPPORT | OPENBB_REST_SUPPORT
    }

    non_us_external_routes = {
        (policy.market, route.provider)
        for policy in policies
        if policy.market != "US"
        for route in policy.routes
        if route.provider != "replay"
    }

    assert non_us_external_routes <= declared
    assert ("TW", "financial_datasets") not in non_us_external_routes
    assert not any(market == "HK" for market, _ in non_us_external_routes)


def test_external_provider_routes_exactly_match_adapter_declarations() -> None:
    policies = load_provider_policies(PROVIDER_POLICIES)
    configured = {
        RegionalProviderCapability(
            provider=route.provider,
            market=policy.market,
            capability=policy.capability,
            endpoint=endpoint,
        )
        for policy in policies
        for route in policy.routes
        if route.provider != "replay"
        for endpoint in route.endpoints
    }

    assert configured == FINANCIAL_DATASETS_SUPPORT | OPENBB_REST_SUPPORT


def _fixture_capabilities(market: str) -> set[RegionalCapability]:
    manifest = yaml.safe_load(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    capabilities: set[RegionalCapability] = set()
    for fixture in manifest["fixtures"]:
        if fixture["market"] != market:
            continue
        interval = fixture["interval"]
        capabilities.add(
            RegionalCapability.PRICES_DAILY
            if interval == "1d"
            else RegionalCapability.PRICES_INTRADAY
        )
        tags = set(fixture["tags"])
        if tags & {"split", "dividend"}:
            capabilities.add(RegionalCapability.CORPORATE_ACTIONS)
    return capabilities
