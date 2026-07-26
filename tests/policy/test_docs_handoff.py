from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[2]

ARCHITECTURE_DOCS = {
    "README.md",
    "integration-blueprint.md",
    "adr/0001-paper-authority-and-artifact-replay.md",
    "adr/0002-process-dependency-license-isolation.md",
    "adr/0003-unsigned-and-keyless-release-trust.md",
}
RUNBOOK_DOCS = {
    "README.md",
    "artifact-storage.md",
    "core-deployment.md",
    "db-restore.md",
    "dead-letter.md",
    "kill-switch.md",
    "ledger-mismatch.md",
    "observability.md",
    "optional-integrations.md",
    "provider-outage.md",
    "service-oidc-key-rotation.md",
    "supply-chain-release.md",
    "worker-crash.md",
}
HANDOFF_DOCS = (
    *(ROOT / "docs" / "architecture" / path for path in ARCHITECTURE_DOCS),
    ROOT / "docs" / "api" / "README.md",
    *(ROOT / "docs" / "runbooks" / path for path in RUNBOOK_DOCS),
    ROOT / "docs" / "verification" / "p6-handoff-evidence.md",
    ROOT / "schemas" / "README.md",
)
LOCAL_LINK = re.compile(r"\[[^]]+\]\((?!https?://|mailto:)([^)#]+)(?:#[^)]+)?\)")


@pytest.mark.policy
def test_handoff_document_set_is_exact() -> None:
    architecture = {
        path.relative_to(ROOT / "docs" / "architecture").as_posix()
        for path in (ROOT / "docs" / "architecture").rglob("*.md")
    }
    runbooks = {
        path.relative_to(ROOT / "docs" / "runbooks").as_posix()
        for path in (ROOT / "docs" / "runbooks").glob("*.md")
    }
    api = {
        path.relative_to(ROOT / "docs" / "api").as_posix()
        for path in (ROOT / "docs" / "api").glob("*.md")
    }
    verification = {
        path.relative_to(ROOT / "docs" / "verification").as_posix()
        for path in (ROOT / "docs" / "verification").glob("*.md")
    }

    assert architecture == ARCHITECTURE_DOCS
    assert runbooks == RUNBOOK_DOCS
    assert api == {"README.md"}
    assert verification == {"p6-handoff-evidence.md"}
    assert all(path.is_file() for path in HANDOFF_DOCS)


@pytest.mark.policy
def test_handoff_local_links_resolve() -> None:
    broken: list[str] = []
    for document in HANDOFF_DOCS:
        for target in LOCAL_LINK.findall(document.read_text(encoding="utf-8")):
            resolved = (document.parent / unquote(target)).resolve()
            if not resolved.exists() or not resolved.is_relative_to(ROOT):
                broken.append(f"{document.relative_to(ROOT)} -> {target}")

    assert broken == []


@pytest.mark.policy
def test_architecture_decisions_and_status_are_explicit() -> None:
    index = (ROOT / "docs" / "architecture" / "README.md").read_text(encoding="utf-8")
    blueprint = (ROOT / "docs" / "architecture" / "integration-blueprint.md").read_text(
        encoding="utf-8"
    )

    assert index.count("adr/000") == 3
    assert "implemented" in index
    assert "configured" in index
    assert "externally_verified" in index
    assert "configured: public repository" in index
    assert "externally_verified: GitHub Actions CI" in index
    for document in ARCHITECTURE_DOCS - {"README.md"}:
        assert index.count(document) == 1
    assert "待一次性確認後進入實作" not in blueprint
    assert "P6.7 的 TLS" not in (
        ROOT / "docs" / "runbooks" / "service-oidc-key-rotation.md"
    ).read_text(encoding="utf-8")


@pytest.mark.policy
def test_runbook_index_references_every_operator_document_once() -> None:
    index = (ROOT / "docs" / "runbooks" / "README.md").read_text(encoding="utf-8")

    for document in RUNBOOK_DOCS - {"README.md"}:
        assert index.count(f"({document})") == 1


@pytest.mark.policy
def test_p6_evidence_index_maps_every_gate_without_forging_external_proof() -> None:
    evidence = (ROOT / "docs" / "verification" / "p6-handoff-evidence.md").read_text(
        encoding="utf-8"
    )

    for phase in range(1, 12):
        assert f"| P6.{phase} |" in evidence
    for job in (
        "verify",
        "postgres",
        "core-deployment",
        "s3-artifact",
        "resilience",
        "capacity",
        "supply-chain",
        "optional-integration-manifests",
    ):
        assert f"`{job}`" in evidence
    for artifact in (
        "resilience-report-${{ github.run_id }}",
        "capacity-report-${{ github.run_id }}",
    ):
        assert f"`{artifact}`" in evidence
    assert "formal `v0.1.2` keyless release已在下列exact" in evidence
    assert "formal publication成功前仍只宣稱configured" not in evidence
    assert "正式`v0.1.2`成功前仍維持fail closed" not in evidence
    assert "`v0.1.0`嘗試已在Cosign v3驗證階段fail closed" in evidence
    assert "GitHub Actions CI、unsigned" in evidence
    for evidence_ref in (
        "30194459987",
        "30194459983",
        "30196542394",
        "30199745730",
        "sha256:068e41e374faf4d3752332bbb91f80b62060990c598f6e34062567a55fe122ca",
        "sha256:dc7566fc578cf49e79a2aadbf316e8e1430b463ec273939db17d97c7f73832c3",
        "optional-profile-smoke-30194459987",
        "unsigned-supply-chain-candidate",
        "5e9c2973b782cd1bd7274e6e6852cbe1df08a4f9",
        "30200612158",
        "30200612154",
        "30200908948",
        "8631582545",
        "8631709866",
        "sha256:9c61a2d5dd59d07d30318b483a7a205ac8af394236662b45021574e42ff19976",
        "823dc70999557c770e7c1cd5c7857cf0d9e155147743435a5013a38a98b85434",
        "8015b3e11470987b6760f480bd208f9c84c08f476205fde0276ff3b2ad65570e",
    ):
        assert f"`{evidence_ref}`" in evidence
    assert "https://github.com/RX5950XT/stonks-agent/releases/tag/v0.1.2" in evidence
    for check in (
        "tests/policy/test_docs_handoff.py",
        "tests/policy/test_api_docs.py",
        "tests/policy/test_release_supply_chain.py",
        "tests/unit/test_release_verifier_final.py",
        "tests/security/test_optional_integrations.py",
    ):
        assert f"`{check}`" in evidence


@pytest.mark.policy
def test_agent_instruction_files_are_identical() -> None:
    assert (ROOT / "AGENTS.md").read_bytes() == (ROOT / "CLAUDE.md").read_bytes()
