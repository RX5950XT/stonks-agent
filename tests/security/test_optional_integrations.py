from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

from stonks_agent.config.features import load_optional_feature_catalog

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "infra" / "compose.optional.yaml"
CORE_DENIED = {
    "nautilus-trader",
    "openbb",
    "pyqlib",
    "rdagent",
    "torch",
}
EXPECTED_SERVICE_PROFILES = {
    "kronos-cpu": "kronos-cpu",
    "kronos-cuda": "kronos-cuda",
    "lean": "lean",
    "nautilus": "nautilus",
    "openbb": "openbb",
    "quant-lab": "qlib",
    "rd-agent-factor-sandbox": "rd-agent",
    "tradingagents-backtest": "tradingagents-backtest",
    "tradingagents-paper": "tradingagents-paper",
    "tradingagents-production": "tradingagents-production",
}


def test_optional_compose_has_exact_profiles_and_zero_default_services() -> None:
    catalog = load_optional_feature_catalog(ROOT / "config" / "features.yaml")
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]
    expected_profiles = {
        profile
        for integration in catalog.deployable_integrations
        for profile in integration.compose_profiles
    }
    actual_profiles: set[str] = set()

    assert compose["name"] == "stonks-optional"
    assert {
        name: service["profiles"][0] for name, service in services.items()
    } == EXPECTED_SERVICE_PROFILES
    assert {"core", "postgres", "redis", "broker"}.isdisjoint(services)
    for service in services.values():
        assert len(service["profiles"]) == 1
        actual_profiles.update(service["profiles"])
        assert "depends_on" not in service
        source = ROOT / "infra" / service["extends"]["file"]
        assert source.is_file()
    assert actual_profiles == expected_profiles


def test_future_rfc_entries_have_no_profile_image_or_dependency() -> None:
    catalog = load_optional_feature_catalog(ROOT / "config" / "features.yaml")

    for integration in catalog.future_rfc_integrations:
        assert integration.enabled is False
        assert integration.compose_profiles == ()
        assert integration.required_environment == ()
        assert integration.supply_chain is None


def test_core_lock_and_project_remain_free_of_optional_heavy_runtimes() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8").lower()
    dependencies = {
        value.split("[", 1)[0].split("<", 1)[0].split(">", 1)[0].lower()
        for value in project["project"]["dependencies"]
    }

    assert dependencies.isdisjoint(CORE_DENIED)
    for denied in CORE_DENIED:
        assert f'name = "{denied}"' not in lock
