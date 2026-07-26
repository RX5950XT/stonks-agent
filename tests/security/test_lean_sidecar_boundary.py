from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SIDECAR = ROOT / "sidecars" / "lean"


def test_lean_runtime_is_isolated_from_core_and_authority_ports() -> None:
    core_dependency_files = (
        (ROOT / "pyproject.toml").read_text("utf-8"),
        (ROOT / "uv.lock").read_text("utf-8"),
    )
    forbidden_imports = {
        "stonks_agent.adapters.postgres",
        "stonks_agent.domain.risk",
        "stonks_agent.ports.execution",
        "stonks_agent.ports.ledger",
        "stonks_agent.ports.repository",
        "stonks_agent.ports.unit_of_work",
    }

    assert all("QuantConnect" not in item for item in core_dependency_files)
    for path in SIDECAR.glob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                imports.add(node.module)
            elif isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
        assert imports.isdisjoint(forbidden_imports)
        assert not any(
            name == "stonks_agent" or name.startswith("stonks_agent.")
            for name in imports
        )
        source = path.read_text("utf-8").lower()
        assert not any(
            marker in source
            for marker in ("database_url", "postgresql", "redis_url", "broker_token")
        )
    assert "subprocess" not in (SIDECAR / "adapter.py").read_text("utf-8")
    assert "subprocess" not in (SIDECAR / "app.py").read_text("utf-8")


def test_lean_build_runtime_and_source_are_exactly_pinned() -> None:
    dockerfile = (SIDECAR / "Dockerfile").read_text("utf-8")
    notice = (SIDECAR / "NOTICE.md").read_text("utf-8")
    distribution = yaml.safe_load(
        (SIDECAR / "distribution-manifest.yaml").read_text("utf-8")
    )
    manifest = yaml.safe_load(
        (ROOT / "docs" / "legal" / "upstream-manifest.yaml").read_text("utf-8")
    )
    upstream = next(item for item in manifest["upstreams"] if item["id"] == "lean")

    assert upstream["snapshot"] == "c22774e49ee80ecef5ca84f57616f6b66fad8bc5"
    assert upstream["license"]["expression"] == "Apache-2.0"
    assert upstream["adoption"]["in_core_allowed"] is False
    assert distribution["source"]["commit"] == upstream["snapshot"]
    assert distribution["source"]["archive_sha256"] == (
        "258a6db94c942e77488e47cb8e5c873c2cc2cbc7e4bc1bce5d14ea4f31b6628f"
    )
    assert (
        distribution["source"]["license_sha256"]
        == (upstream["license"]["evidence"][0]["sha256"])
    )
    assert (
        "sha256:ed034a8bf0b24ded0cbbac07e17825d8e9ebfe21e308191d0f7421eaf5ad4664"
        in dockerfile
    )
    assert (
        "sha256:ed5d539b27842d656a06a5984dbcb5114d3e885fbada612a49a5a7c3c3a44e1c"
        in dockerfile
    )
    assert (
        "dotnet list Launcher/QuantConnect.Lean.Launcher.csproj package" in dockerfile
    )
    assert "--vulnerable --include-transitive --format json" in dockerfile
    assert "! grep -q '\"vulnerabilities\"'" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert "/usr/share/source/lean" in dockerfile
    assert "Apache-2.0" in notice
    assert upstream["snapshot"] in notice
    assert distribution["verification_tools"] == {
        "syft_image": "anchore/syft@sha256:86fde6445b483d902fe011dd9f68c4987dd94e07da1e9edc004e3c2422650de6",
        "grype_image": "anchore/grype@sha256:391bfda62888fb4e98ff5c4c81598f7431a3c1eac3f8519d69d1ff00df247c1d",
        "policy": "fail on high or critical vulnerabilities",
    }


def test_lean_template_is_backtest_only_and_has_no_live_credentials() -> None:
    template_text = (SIDECAR / "appsettings.template.json").read_text("utf-8")
    template = json.loads(template_text)

    assert template["live-mode"] is False
    assert template["algorithm-type-name"] == "Stonks.Lean.CanonicalBacktestAlgorithm"
    assert (
        template["messaging-handler"] == "QuantConnect.Messaging.EventMessagingHandler"
    )
    assert template["result-handler"] == (
        "QuantConnect.Lean.Engine.Results.BacktestingResultHandler"
    )
    forbidden = re.compile(
        r"brokerage|api-access-token|password|secret|live-data|account-id",
        re.IGNORECASE,
    )
    assert forbidden.search(template_text) is None


def test_lean_compose_is_internal_non_root_and_resource_bounded() -> None:
    compose = yaml.safe_load((ROOT / "infra" / "compose.lean.yaml").read_text("utf-8"))
    service = compose["services"]["lean"]

    assert compose["networks"]["lean-internal"]["internal"] is True
    assert service["networks"] == ["lean-internal"]
    assert service["user"] == "65532:65532"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["pids_limit"] == 256
    assert service["mem_limit"] == "3g"
    assert service["cpus"] == 2.0
    assert service["ports"] == ["127.0.0.1:7410:7410"]
    assert service["tmpfs"] == [
        "/tmp:rw,noexec,nosuid,nodev,size=512m,uid=65532,gid=65532,mode=0700"
    ]


def test_vulnerability_patches_remove_unsafe_dependency_chains() -> None:
    patches = "\n".join(
        path.read_text("utf-8")
        for path in sorted((SIDECAR / "patches").glob("*.patch"))
    )
    compat = (SIDECAR / "engine" / "IonicZipCompat.cs").read_text("utf-8")

    assert '-    <PackageReference Include="DotNetZip" Version="1.16.0" />' in patches
    assert '-    <PackageReference Include="NetMQ" Version="4.0.1.6" />' in patches
    assert 'Compile Remove="Messaging.cs;StreamingMessageHandler.cs"' in patches
    assert "System.IO.Compression" in compat
    assert "Ionic.Zip" in compat


def test_distribution_manifest_hashes_modified_sources_and_lock_graph() -> None:
    distribution = yaml.safe_load(
        (SIDECAR / "distribution-manifest.yaml").read_text("utf-8")
    )
    for item in distribution["modifications"]:
        assert (
            hashlib.sha256((SIDECAR / item["path"]).read_bytes()).hexdigest()
            == (item["sha256"])
        )
    digest = hashlib.sha256()
    for path in sorted((SIDECAR / "dotnet-locks").rglob("*")):
        if not path.is_file():
            continue
        digest.update(path.relative_to(SIDECAR).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    assert digest.hexdigest() == distribution["build"]["nuget_lock_tree_sha256"]


def test_runtime_hash_covers_python_csharp_patches_template_and_locks(
    tmp_path: Path,
) -> None:
    from sidecars.lean.adapter import compute_runtime_hash

    runtime_hash = compute_runtime_hash(SIDECAR)
    assert re.fullmatch(r"[0-9a-f]{64}", runtime_hash)

    clone = tmp_path / "sidecars" / "lean"
    shutil.copytree(
        SIDECAR,
        clone,
        ignore=shutil.ignore_patterns(".venv", "__pycache__", "tests"),
    )
    shutil.copytree(
        ROOT / "packages" / "contracts", tmp_path / "packages" / "contracts"
    )
    previous = compute_runtime_hash(clone)
    for relative in (
        Path("appsettings.template.json"),
        Path("engine/CanonicalBacktestAlgorithm.cs"),
        Path("patches/0001-remove-vulnerable-runtime-dependencies.patch"),
        Path("dotnet-locks/Launcher/packages.lock.json"),
    ):
        path = clone / relative
        path.write_bytes(path.read_bytes() + b"\n")
        current = compute_runtime_hash(clone)
        assert current != previous
        previous = current
