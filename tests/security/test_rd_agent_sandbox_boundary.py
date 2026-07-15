from __future__ import annotations

import ast
import json
import shutil
import tomllib
from pathlib import Path

import yaml

from stonks_agent.application.evaluation.rd_agent import (
    aggregate_sandbox_runs,
    evaluate_rd_agent_candidate,
)
from stonks_contracts.rd_agent import (
    CandidateSandboxPolicy,
    RDAgentCandidateKind,
    RDSandboxJob,
    RDSandboxResult,
    RDSandboxRunResult,
)

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "workers" / "quant_lab" / "rd_agent"
CORE = ROOT / "src" / "stonks_agent" / "application" / "evaluation" / "rd_agent.py"
EXPECTED_PYTHON_VEX = {
    "CVE-2026-3298",
    "CVE-2026-3644",
    "CVE-2026-4224",
    "CVE-2026-4786",
    "CVE-2026-6100",
    "CVE-2026-7210",
    "CVE-2026-9669",
    "CVE-2026-11940",
    "CVE-2026-11972",
    "CVE-2026-15308",
}


def test_core_aggregator_has_no_worker_runtime_or_control_plane_authority() -> None:
    forbidden = (
        "docker",
        "subprocess",
        "workers",
        "stonks_agent.adapters",
        "stonks_agent.application.execution",
        "stonks_agent.application.ledger",
        "stonks_agent.application.portfolio",
        "stonks_agent.application.risk",
        "stonks_agent.ports.execution",
        "stonks_agent.ports.ledger",
        "stonks_agent.ports.repository",
        "stonks_agent.ports.unit_of_work",
    )
    imports = tuple(_imports(ast.parse(CORE.read_text("utf-8"))))

    assert not any(
        value == denied or value.startswith(f"{denied}.")
        for value in imports
        for denied in forbidden
    )
    assert aggregate_sandbox_runs.__module__.startswith("stonks_agent.application")
    assert evaluate_rd_agent_candidate.__module__.startswith("stonks_agent.application")


def test_factor_contracts_cannot_authorize_order_target_or_promotion() -> None:
    fields = (
        frozenset(RDSandboxJob.model_fields)
        | frozenset(RDSandboxRunResult.model_fields)
        | frozenset(RDSandboxResult.model_fields)
    )
    forbidden = {
        "account_reservation",
        "execution_receipt",
        "order",
        "order_intent",
        "portfolio_target",
        "preferred_candidate",
        "risk_decision",
        "target",
    }
    job_schema = RDSandboxJob.model_json_schema()["properties"]
    result_schema = RDSandboxResult.model_json_schema()["properties"]

    assert fields.isdisjoint(forbidden)
    assert job_schema["promotion_allowed"]["const"] is False
    assert result_schema["deterministic"]["const"] is True
    assert tuple(RDAgentCandidateKind) == (RDAgentCandidateKind.FACTOR,)


def test_sandbox_policy_declares_every_enforced_high_risk_boundary() -> None:
    payload = yaml.safe_load((WORKER / "sandbox_policy.yaml").read_text("utf-8"))
    policy = CandidateSandboxPolicy.model_validate(payload["sandbox"])

    assert policy.platform == "linux"
    assert policy.network_mode == "none"
    assert policy.root_filesystem == "read_only"
    assert policy.dataset_mount == "read_only"
    assert policy.source_mount == "read_only"
    assert policy.isolation_scope == "fresh_container_per_repetition"
    assert policy.run_as_uid == policy.run_as_gid == 65532
    assert policy.capability_mode == "drop_all"
    assert policy.no_new_privileges is True
    assert policy.device_access == policy.unix_socket_access == "none"
    assert policy.host_namespace_mode == "private"
    assert policy.fixed_argv is True and policy.shell_allowed is False
    assert policy.promotion_allowed is False


def test_compose_is_one_shot_no_network_no_mount_and_resource_bounded() -> None:
    compose = yaml.safe_load(
        (ROOT / "infra" / "compose.rd-agent.yaml").read_text("utf-8")
    )
    service = compose["services"]["rd-agent-factor-sandbox"]

    assert service["network_mode"] == "none"
    assert service["user"] == "65532:65532"
    assert service["read_only"] is True
    assert service["privileged"] is False
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert "apparmor=docker-default" in service["security_opt"]
    assert service["ipc"] == "private"
    assert service["pids_limit"] <= 16
    assert service["mem_limit"] == "256m"
    assert service["cpus"] == 1.0
    assert {"noexec", "nosuid", "nodev"} <= set(service["tmpfs"][0].split(","))
    assert set(service["environment"]) == {
        "STONKS_RD_RUNTIME_HASH",
        "STONKS_RD_IMAGE_DIGEST",
    }
    assert "volumes" not in service
    assert "devices" not in service
    assert "ports" not in service


def test_image_preserves_pinned_source_but_never_imports_or_executes_it() -> None:
    dockerfile = (WORKER / "Dockerfile").read_text("utf-8")
    manifest = yaml.safe_load(
        (WORKER / "distribution-manifest.yaml").read_text("utf-8")
    )

    assert manifest["commit"] == "4f9ecb005881cddc08df0124a2e894c018007679"
    assert manifest["license"] == "MIT"
    assert manifest["source"]["archive_sha256"] in dockerfile
    assert manifest["source"]["license_sha256"] in dockerfile
    assert manifest["build"]["upstream_on_pythonpath"] is False
    assert manifest["build"]["upstream_executed"] is False
    assert manifest["build"]["upstream_docker_socket_allowed"] is False
    assert "USER 65532:65532" in dockerfile
    assert 'CMD ["python", "-m", "workers.quant_lab.rd_agent.cli"]' in dockerfile
    assert "docker.sock" not in dockerfile
    assert "pip install /opt/rd-agent" not in dockerfile
    assert "PYTHONPATH=/opt/rd-agent" not in dockerfile


def test_openvex_is_exact_and_grype_has_no_manual_ignores() -> None:
    config = yaml.safe_load((WORKER / "grype.yaml").read_text("utf-8"))
    document = json.loads((WORKER / "openvex.json").read_text("utf-8"))
    statements = document["statements"]

    assert config == {"ignore": []}
    assert document["@context"] == "https://openvex.dev/ns/v0.2.0"
    assert len(statements) == len(EXPECTED_PYTHON_VEX)
    assert {
        statement["vulnerability"]["name"] for statement in statements
    } == EXPECTED_PYTHON_VEX
    for statement in statements:
        assert statement["products"] == [{"@id": "pkg:generic/python@3.12.13"}]
        assert statement["status"] == "not_affected"
        assert statement["justification"] == "vulnerable_code_not_present"


def test_runtime_image_removes_every_reviewed_vulnerable_capability() -> None:
    dockerfile = (WORKER / "Dockerfile").read_text("utf-8")
    expected_removed_paths = {
        "asyncio/windows_events.py",
        "asyncio/windows_utils.py",
        "bz2.py",
        "gzip.py",
        "html",
        "http/cookies.py",
        "lib-dynload/_bz2*.so",
        "lib-dynload/_elementtree*.so",
        "lib-dynload/_lzma*.so",
        "lib-dynload/_sqlite3*.so",
        "lib-dynload/pyexpat*.so",
        "lzma.py",
        "sqlite3",
        "tarfile.py",
        "webbrowser.py",
        "xml",
    }

    assert expected_removed_paths <= {
        line.strip().removesuffix("\\").strip()
        for line in dockerfile.splitlines()
        if line.startswith("        ")
    }
    assert "apk del --no-network .python-rundeps sqlite-libs" in dockerfile
    assert "site-packages/pip" in dockerfile


def test_supply_chain_review_files_are_runtime_hash_bound() -> None:
    from workers.quant_lab.rd_agent.adapter import RUNTIME_FILES

    assert {"CVE_REVIEW.md", "grype.yaml", "openvex.json"} <= set(RUNTIME_FILES)
    manifest = yaml.safe_load(
        (WORKER / "distribution-manifest.yaml").read_text("utf-8")
    )
    assert manifest["verification"]["cve_policy"] == ("fail-on-high-with-exact-vex")
    assert manifest["verification"]["reviewed_vex"] == sorted(EXPECTED_PYTHON_VEX)


def test_child_and_core_dependencies_exclude_heavy_or_unsafe_runtimes() -> None:
    worker_project = tomllib.loads((WORKER / "pyproject.toml").read_text("utf-8"))
    core_project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    dependencies = tuple(worker_project["project"]["dependencies"])
    core_dependencies = tuple(core_project["project"]["dependencies"])
    denied = (
        "docker",
        "numpy",
        "pandas",
        "pyqlib",
        "rdagent",
        "torch",
    )

    assert not any(
        value.lower().startswith(prefix) for value in dependencies for prefix in denied
    )
    assert not any(
        value.lower().startswith(prefix)
        for value in core_dependencies
        for prefix in denied
    )
    child_imports = tuple(
        _imports(ast.parse((WORKER / "candidate_runner.py").read_text("utf-8")))
    )
    assert {"ctypes", "pickle", "socket", "subprocess"}.isdisjoint(child_imports)


def test_runtime_hash_covers_every_execution_file(tmp_path: Path) -> None:
    from workers.quant_lab.rd_agent.adapter import RUNTIME_FILES, compute_runtime_hash

    clone = tmp_path / "rd_agent"
    shutil.copytree(WORKER, clone)
    original = compute_runtime_hash(clone)

    for relative in RUNTIME_FILES:
        target = clone / relative
        before = target.read_bytes()
        target.write_bytes(before + b"\n")
        assert compute_runtime_hash(clone) != original
        target.write_bytes(before)


def _imports(tree: ast.AST) -> tuple[str, ...]:
    values: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            values.append(node.module)
        elif isinstance(node, ast.Import):
            values.extend(item.name for item in node.names)
    return tuple(values)
