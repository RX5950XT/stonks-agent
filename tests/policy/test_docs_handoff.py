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
    assert "externally_verified: private GitHub Actions" in index
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
    assert "protected tag publication: 未驗證" in evidence
    assert "formal keyless signature / provenance: 未產生" in evidence
    assert "private GitHub Actions CI: 已驗證" in evidence
    for evidence_ref in (
        "30194459987",
        "30194459983",
        "optional-profile-smoke-30194459987",
        "unsigned-supply-chain-candidate",
    ):
        assert f"`{evidence_ref}`" in evidence
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
