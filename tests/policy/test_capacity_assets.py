from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

from stonks_agent.config.capacity import load_capacity_policy

ROOT = Path(__file__).resolve().parents[2]
POSTGRES_IMAGE = (
    "postgres:17.10-alpine@sha256:"
    "742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"
)


def test_capacity_runbook_states_measured_boundary_and_stop_conditions() -> None:
    content = (ROOT / "docs" / "operations" / "capacity.md").read_text(encoding="utf-8")

    for token in (
        "paper-only",
        "single_host_ci_baseline",
        "probe_process",
        "probe_runtime_budget",
        "static_manifest_only",
        "未實測",
        "CUDA CI未量測GPU/VRAM",
        "production_sla_claim=false",
        "停止條件",
        "business API",
        "dispatcher",
        "GPU/VRAM",
    ):
        assert token in content, f"capacity.md: missing {token}"


def test_capacity_ci_uses_test_only_postgres_and_read_only_authority() -> None:
    path = ROOT / ".github" / "workflows" / "ci.yml"
    content = path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(content)
    job = workflow["jobs"]["capacity"]
    service = job["services"]["postgres"]
    audit_step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Audit heavy-worker frozen runtime dependencies"
    )
    audit_commands = " ".join(str(audit_step["run"]).replace("\\", "").split())

    assert job["permissions"] == {"contents": "read"}
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] <= 15
    assert service["image"] == POSTGRES_IMAGE
    assert service["env"]["POSTGRES_DB"] == "stonks_capacity"
    assert "STONKS_CAPACITY_DATABASE_URL" in job["env"]
    assert "stonks_capacity" in job["env"]["STONKS_CAPACITY_DATABASE_URL"]
    assert "scripts/run_capacity_probe.py" in content
    assert "capacity-report.json" in content
    assert "tests/performance" in content
    assert "--output" in content
    for project in (
        "workers/tradingagents",
        "workers/kronos",
        "workers/kronos/profiles/cuda",
        "workers/quant_lab",
    ):
        assert f"uv lock --check --project {project}" in content
        assert f"scripts/audit_python_project.py --project {project}" in audit_commands
    assert content.count("--standard-identity-package torch") == 2
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in content
    assert "id-token: write" not in str(job)
    assert "packages: write" not in str(job)


def test_static_process_budgets_match_core_runtime_manifests() -> None:
    policy = load_capacity_policy(ROOT / "config" / "capacity.yaml")
    budgets = {item.process_id.value: item for item in policy.process_budgets}
    compose = yaml.safe_load((ROOT / "infra" / "compose.yaml").read_text("utf-8"))

    for process_id, service_id in (("core", "core"), ("postgres", "postgres")):
        budget = budgets[process_id]
        service = compose["services"][service_id]
        assert budget.cpu_millicores_ceiling == int(float(service["cpus"]) * 1000)
        assert budget.ram_mebibytes_ceiling == _memory_mebibytes(service["mem_limit"])
        assert budget.pid_ceiling == service["pids_limit"]

    deployment = (ROOT / "src/stonks_agent/entrypoints/deployment.py").read_text(
        "utf-8"
    )
    assert "workers=1" in deployment
    assert "limit_concurrency=128" in deployment
    assert deployment.count("connection limit 16") == 2
    assert budgets["core"].process_ceiling == 1
    assert budgets["core"].in_flight_ceiling == 128
    assert budgets["postgres"].in_flight_ceiling == 16


def _memory_mebibytes(value: object) -> int:
    assert isinstance(value, str) and value.endswith("m")
    return int(value[:-1])


def test_performance_tests_use_an_explicit_registered_marker() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    markers = project["tool"]["pytest"]["ini_options"]["markers"]

    assert any(item.startswith("performance:") for item in markers)
