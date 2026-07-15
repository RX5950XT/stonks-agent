from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from stonks_agent.config.features import (
    DEPLOYABLE_INTEGRATIONS,
    FUTURE_RFC_INTEGRATIONS,
    IntegrationName,
    OptionalFeaturesLoadError,
    load_optional_feature_catalog,
    load_optional_feature_flags,
)

ROOT = Path(__file__).resolve().parents[2]


def test_missing_optional_catalog_keeps_every_core_feature_disabled(
    tmp_path: Path,
) -> None:
    flags = load_optional_feature_flags(tmp_path / "not-configured.yaml")

    assert flags.any_enabled is False
    assert flags.enabled_integrations == ()
    assert all(flags.is_enabled(name) is False for name in IntegrationName)


def test_no_optional_catalog_path_keeps_every_core_feature_disabled() -> None:
    flags = load_optional_feature_flags(None)

    assert flags.any_enabled is False
    assert flags.enabled_integrations == ()


def test_committed_catalog_is_complete_default_off_and_paper_only() -> None:
    catalog = load_optional_feature_catalog(ROOT / "config" / "features.yaml")

    assert catalog.execution_mode == "paper"
    assert tuple(item.name for item in catalog.integrations) == tuple(IntegrationName)
    assert catalog.flags.any_enabled is False
    assert {item.name for item in catalog.deployable_integrations} == set(
        DEPLOYABLE_INTEGRATIONS
    )
    assert {item.name for item in catalog.future_rfc_integrations} == set(
        FUTURE_RFC_INTEGRATIONS
    )
    assert all(item.affects_core_readiness is False for item in catalog.integrations)
    assert all(item.execution_authority is False for item in catalog.integrations)
    for integration in catalog.integrations:
        for relative in integration.config_paths:
            assert (ROOT / relative).is_file(), relative


def test_deployable_supply_chain_paths_exist_and_core_dependency_is_denied() -> None:
    catalog = load_optional_feature_catalog(ROOT / "config" / "features.yaml")

    for integration in catalog.deployable_integrations:
        assert integration.compose_profiles
        assert integration.supply_chain is not None
        supply_chain = integration.supply_chain
        assert supply_chain.core_dependency_allowed is False
        assert supply_chain.images
        assert supply_chain.lock_paths
        assert supply_chain.notice_paths
        for relative in supply_chain.lock_paths + supply_chain.notice_paths:
            assert (ROOT / relative).is_file(), relative


@pytest.mark.parametrize(
    "payload",
    [
        "schema_version: 1\nexecution_mode: live\nintegrations: []\n",
        "schema_version: 1\nexecution_mode: paper\nintegrations: []\n",
        (
            "schema_version: 1\nexecution_mode: paper\nintegrations:\n"
            "  - {name: unknown, enabled: true}\n"
        ),
    ],
)
def test_malformed_unknown_or_live_catalog_fails_closed(
    tmp_path: Path,
    payload: str,
) -> None:
    path = tmp_path / "features.yaml"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(OptionalFeaturesLoadError) as raised:
        load_optional_feature_catalog(path)

    assert raised.value.error.code.value == "configuration_invalid"
    assert raised.value.error.details == {"file": "features.yaml"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("compose_profiles", ["qlib"]),
        ("required_environment", ["AWS_SECRET_ACCESS_KEY"]),
        ("network_policy", "external_https"),
        ("output_scope", "research_artifact"),
    ],
)
def test_changed_openbb_boundary_fails_closed(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload = yaml.safe_load(
        (ROOT / "config" / "features.yaml").read_text(encoding="utf-8")
    )
    payload["integrations"][1][field] = value
    path = tmp_path / "features.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(OptionalFeaturesLoadError):
        load_optional_feature_catalog(path)
