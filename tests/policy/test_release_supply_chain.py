from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def _workflow(name: str) -> tuple[str, dict[str, object]]:
    path = ROOT / ".github" / "workflows" / name
    return path.read_text(encoding="utf-8"), yaml.safe_load(
        path.read_text(encoding="utf-8")
    )


def test_all_workflow_actions_use_immutable_full_commit_shas() -> None:
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        content = path.read_text(encoding="utf-8")
        for reference in re.findall(r"^\s*uses:\s*([^@\s]+)@([^\s#]+)", content, re.M):
            action, revision = reference
            assert FULL_SHA.fullmatch(revision), f"{path}: {action}@{revision}"


def test_security_workflow_has_read_only_fork_safe_supply_chain_gates() -> None:
    content, workflow = _workflow("security.yml")

    assert "pull_request_target" not in content
    assert workflow["permissions"] == {"contents": "read"}
    assert "enable-cache: false" in content
    assert "@sha256:" in content
    assert "scripts/generate_sbom.py" in content
    assert "scripts/generate_alpine_source.py" in content
    assert "scripts/generate_python_source.py" in content
    assert "scripts/verify_release.py" in content
    assert "scripts/check_secrets.py" in content
    assert "scripts/check_upstream_policy.py" in content
    assert "--fail-on high" in content
    assert "/var/run/docker.sock" not in content
    assert "docker-archive:/out/core-image.tar" in content
    assert "SYFT_CHECK_FOR_APP_UPDATE=false" in content
    assert "sbom:/out/core.raw.cdx.json" in content
    assert "id-token: write" not in content
    assert "packages: write" not in content


def test_release_workflow_is_tag_only_and_keyless_authority_is_scoped() -> None:
    content, workflow = _workflow("release.yml")
    assert "pull_request" not in content
    assert "workflow_run" not in content
    assert "tags:" in content and "v*.*.*" in content
    assert workflow["permissions"] == {}
    assert "github.ref_protected" in content
    assert "environment: release" in content
    assert "id-token: write" in content
    assert "cosign-release: v3.0.6" in content
    assert "cosign sign --yes" in content
    assert content.count("cosign sign-blob --yes") == 2
    assert "cosign verify" in content
    assert "cosign verify-blob" in content
    assert "verification-report.sigstore.json" in content
    assert "certificate-oidc-issuer" in content
    assert "https://token.actions.githubusercontent.com" in content
    assert "--certificate-github-workflow-repository" in content
    assert "--certificate-github-workflow-ref" in content
    assert "--certificate-github-workflow-sha" in content
    assert "--certificate-github-workflow-trigger" in content
    assert "docker push" in content
    assert "@${IMAGE_DIGEST}" in content or "@$IMAGE_DIGEST" in content
    assert "secrets.GITHUB_TOKEN" not in content
    assert "GH_TOKEN: ${{ github.token }}" in content
    assert content.count("docker/login-action@") == 2
    assert "mapfile -t digests" in content
    assert '[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]' in content
    assert 'gh release verify "$GITHUB_REF_NAME"' in content


def test_release_jobs_hold_only_their_required_authority() -> None:
    _, workflow = _workflow("release.yml")
    jobs = workflow["jobs"]

    assert jobs["gate"]["permissions"] == {"contents": "read"}
    assert jobs["authorize"]["permissions"] == {}
    assert jobs["build-scan"]["permissions"] == {"contents": "read"}
    assert jobs["publish-image"]["permissions"] == {"packages": "write"}
    assert jobs["attest-sign"]["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }
    assert jobs["github-release"]["permissions"] == {"contents": "write"}


def test_release_verifies_the_unsigned_candidate_before_registry_publication() -> None:
    content, _ = _workflow("release.yml")
    push_offset = content.index('docker push "$IMAGE:$GITHUB_REF_NAME"')
    create_offset = content.index("scripts/verify_release.py create")
    verify_offset = content.index("scripts/verify_release.py verify")

    assert create_offset < verify_offset < push_offset
    assert content.count("scripts/verify_release.py create") == 2
    assert content.count("scripts/verify_release.py verify") == 2


def test_core_and_openbb_images_receive_exact_release_identity_build_args() -> None:
    for name in ("security.yml", "release.yml"):
        content, _ = _workflow(name)
        assert content.count("--build-arg VCS_REF=") == 2
        assert content.count("--build-arg SOURCE_URL=") == 2
        assert content.count("--build-arg RELEASE_VERSION=") == 2


def test_grype_commands_share_an_explicit_host_cache_and_fail_closed() -> None:
    for name in ("security.yml", "release.yml"):
        content, _ = _workflow(name)
        assert "set -euo pipefail" in content
        assert 'mkdir -p "$RUNNER_TEMP/grype-cache"' in content
        assert content.count("GRYPE_DB_CACHE_DIR=/grype-db") == 3
        assert (
            content.count("type=bind,source=$RUNNER_TEMP/grype-cache,target=/grype-db")
            == 3
        )
        assert "db update" in content
        assert "db status --output json > " in content
        assert content.count("GRYPE_DB_AUTO_UPDATE=false") == 2
        assert content.count("GRYPE_CHECK_FOR_APP_UPDATE=false") == 3


def test_release_policy_is_closed_paper_only_and_scanners_are_digest_pinned() -> None:
    policy = json.loads(
        (ROOT / "config" / "release-policy.json").read_text(encoding="utf-8")
    )

    assert policy["schema_version"] == "stonks-agent/release-policy/v1"
    assert policy["release"]["execution_mode"] == "paper"
    assert policy["vulnerabilities"]["blocked_severities"] == ["High", "Critical"]
    assert DIGEST.search(policy["tools"]["syft_image"])
    assert DIGEST.search(policy["tools"]["grype_image"])
    assert policy["signing"]["issuer"] == "https://token.actions.githubusercontent.com"
    assert policy["signing"]["verification_report_bundle"] == (
        "signatures/verification-report.sigstore.json"
    )
    assert policy["sbom"]["expected_components_sha256"] == (
        "b1584f2aa0b10fca2d330972d73fa0255d1e94dd627c0c5da6700fb2d6ca6bd4"
    )
    required = set(policy["bundle"]["required_payload_files"])
    assert {
        "payload/LICENSE",
        "payload/THIRD_PARTY_NOTICES.md",
        "payload/release/core.cdx.json",
        "payload/release/core.grype.json",
        "payload/release/alpine-corresponding-source.tar.gz",
        "payload/release/openbb-corresponding-source.tar.gz",
        "payload/release/python-corresponding-source.tar.gz",
        "payload/config/release/python-source-policy.json",
        "payload/scripts/release_source_contracts.py",
    } <= required
    assert policy["python_source"] == {
        "archive": "payload/release/python-corresponding-source.tar.gz",
        "policy": "payload/config/release/python-source-policy.json",
        "uv_lock": "payload/uv.lock",
        "archive_sha256": (
            "2d1fd3e5b14b0d33c49aa25a649aaf8089fa5186ca06ec96713d80d1c120a7d8"
        ),
        "manifest_sha256": (
            "56baa04803cd64469cc446ccb21f8aa1b523d441795cdb51314c944ccf6d7ea6"
        ),
        "source_count": 3,
        "total_source_bytes": 947504,
    }


def test_both_supply_chain_workflows_generate_and_stage_exact_source_closure() -> None:
    for name in ("security.yml", "release.yml"):
        content, _ = _workflow(name)
        assert "scripts/generate_alpine_source.py" in content
        assert "scripts/generate_python_source.py" in content
        assert "alpine-corresponding-source.tar.gz" in content
        assert "python-corresponding-source.tar.gz" in content
        assert '--sandbox-image "$sandbox_image"' in content


def test_openbb_source_archive_and_image_identity_are_deterministic() -> None:
    dockerfile = (ROOT / "sidecars" / "openbb" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "ARG VCS_REF" in dockerfile
    assert 'org.opencontainers.image.source="' in dockerfile
    assert 'org.opencontainers.image.revision="${VCS_REF}"' in dockerfile
    assert "tar --sort=name" in dockerfile
    assert "--mtime=@0" in dockerfile
    assert "--owner=0 --group=0 --numeric-owner" in dockerfile


def test_core_runtime_removes_reviewed_unreachable_vulnerable_capabilities() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "python:3.12.13-alpine3.23@sha256:" in dockerfile
    assert "apk del --no-network .python-rundeps sqlite-libs" in dockerfile
    for path in (
        "asyncio/windows_events.py",
        "asyncio/windows_utils.py",
        "bz2.py",
        "html/parser.py",
        "lib-dynload/_bz2*.so",
        "lib-dynload/_lzma*.so",
        "lib-dynload/_sqlite3*.so",
        "lzma.py",
        "sqlite3",
        "tarfile.py",
        "webbrowser.py",
    ):
        assert path in dockerfile
    assert "patch_cpython_stdlib.py" in dockerfile
    assert "57e88c1cf95e1481b94ae57abe1010469d47a6b4" in (
        ROOT / "scripts" / "patch_cpython_stdlib.py"
    ).read_text(encoding="utf-8")


def test_core_vex_is_exact_and_matches_reviewed_runtime_mitigations() -> None:
    payload = json.loads(
        (ROOT / "config" / "release" / "core.openvex.json").read_text(encoding="utf-8")
    )
    statements = payload["statements"]
    expected = {
        "CVE-2026-11940": "vulnerable_code_not_present",
        "CVE-2026-11972": "vulnerable_code_not_present",
        "CVE-2026-15308": "vulnerable_code_not_present",
        "CVE-2026-3298": "vulnerable_code_not_present",
        "CVE-2026-3644": "vulnerable_code_not_present",
        "CVE-2026-4224": "inline_mitigations_already_exist",
        "CVE-2026-4786": "vulnerable_code_not_present",
        "CVE-2026-6100": "vulnerable_code_not_in_execute_path",
        "CVE-2026-7210": "inline_mitigations_already_exist",
        "CVE-2026-9669": "vulnerable_code_not_present",
    }
    assert {
        statement["vulnerability"]["name"]: statement["justification"]
        for statement in statements
    } == expected
    for statement in statements:
        assert statement["products"] == [{"@id": "pkg:generic/python@3.12.13"}]
        assert statement["status"] == "not_affected"
