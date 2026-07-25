from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

from scripts.smoke_optional_profiles import load_policy
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


def test_optional_smoke_policy_matches_exact_compose_profiles() -> None:
    policy = load_policy(ROOT / "config" / "optional-smoke.yaml")
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    compose_profiles = {
        profile
        for service in compose["services"].values()
        for profile in service["profiles"]
    }

    assert {item.profile for item in policy.profiles} == compose_profiles
    assert (
        sum(
            item.compatibility_expectation == "actual_runtime"
            for item in policy.profiles
        )
        == 4
    )
    assert (
        sum(item.compatibility_expectation == "blocked" for item in policy.profiles)
        == 5
    )
    assert (
        sum(item.compatibility_expectation == "unsupported" for item in policy.profiles)
        == 1
    )


def test_optional_ci_matrix_requires_independent_actual_runtime_jobs() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["optional-integration-manifests"]
    rendered = yaml.safe_dump(job, sort_keys=True)

    assert set(job["needs"]) == {
        "core-deployment",
        "openbb-sidecar",
        "nautilus-sidecar",
        "lean-sidecar",
        "rd-agent-sandbox",
    }
    assert job["permissions"] == {"contents": "read"}
    assert job["timeout-minutes"] == 30
    assert (
        job["env"]["STONKS_OPTIONAL_ACTUAL_RUNTIME_PROFILES"]
        == "openbb,nautilus,lean,rd-agent"
    )
    assert "scripts/smoke_optional_profiles.py" in rendered
    assert "optional-profile-smoke.json" in rendered
    assert "if-no-files-found: error" in rendered
    assert "uv run python -m pytest" in rendered
    assert "uv run pytest" not in rendered
    assert "continue-on-error" not in rendered
