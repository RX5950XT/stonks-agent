from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

from scripts.release_verifier_bundle import stage_release
from scripts.release_verifier_reports import verify_notices

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


def test_unsigned_candidate_derives_release_identity_from_project_version() -> None:
    content, _ = _workflow("security.yml")

    assert 'echo "RELEASE_VERSION=$version" >> "$GITHUB_ENV"' in content
    assert 'echo "RELEASE_TAG=v$version" >> "$GITHUB_ENV"' in content
    assert '--build-arg RELEASE_VERSION="$RELEASE_VERSION"' in content
    assert '--version "$RELEASE_VERSION" --tag "$RELEASE_TAG"' in content
    assert '--expected-tag "$RELEASE_TAG"' in content
    assert "RELEASE_VERSION=0.1.0" not in content
    assert "--version 0.1.0" not in content
    assert "--tag v0.1.0" not in content


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
    assert "cosign verify-attestation" in content
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


def test_cosign_v3_uploads_and_verifies_the_same_exact_image_bundle() -> None:
    content, _ = _workflow("release.yml")
    assert (
        "cosign sign --yes \\\n"
        '            --bundle "$bundle/signatures/core-image.sigstore.json" \\\n'
        '            "$subject"'
    ) in content
    assert (
        "cosign verify-blob-attestation \\\n"
        '            --bundle "$bundle/signatures/core-image.sigstore.json" \\\n'
        '            --digest "${IMAGE_DIGEST#sha256:}" --digestAlg sha256 \\\n'
        "            --type https://sigstore.dev/cosign/sign/v1"
    ) in content
    assert (
        "cosign attach attestation \\\n"
        '            --attestation "$bundle/signatures/core-image.sigstore.json" \\\n'
        '            "$subject"'
    ) in content
    attach_index = content.index("cosign attach attestation \\")
    registry_verify = content[
        attach_index : content.index(
            "uv run python scripts/verify_release.py create", attach_index
        )
    ]
    assert "cosign verify-attestation \\" in registry_verify
    assert "--type https://sigstore.dev/cosign/sign/v1" in registry_verify
    assert "registry_verified=false" in registry_verify
    assert "for attempt in $(seq 1 6); do" in registry_verify
    assert 'test "$registry_verified" = "true"' in registry_verify
    assert registry_verify.count("cosign attach attestation \\") == 1
    assert registry_verify.count("cosign verify-attestation \\") == 1
    assert (
        "--bundle"
        not in registry_verify.split("cosign verify-attestation \\", maxsplit=1)[1]
    )


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


def test_immutable_github_release_can_resume_without_recreating_publication() -> None:
    content, _ = _workflow("release.yml")

    assert 'gh release view "$GITHUB_REF_NAME"' in content
    assert "--json isDraft --jq .isDraft" in content
    assert 'gh release upload "$GITHUB_REF_NAME"' in content
    assert "--clobber" in content
    assert "for attempt in $(seq 1 6)" in content
    assert 'test "$verified" = "true"' in content
    assert content.count("gh release verify-asset") == 2


def test_release_verifies_the_unsigned_candidate_before_registry_publication() -> None:
    content, _ = _workflow("release.yml")
    push_offset = content.index('docker push "$IMAGE:$GITHUB_REF_NAME"')
    create_offset = content.index("scripts/verify_release.py create")
    verify_offset = content.index("scripts/verify_release.py verify")

    assert create_offset < verify_offset < push_offset
    assert content.count("scripts/verify_release.py create") == 2
    assert content.count("scripts/verify_release.py verify") == 3
    assert content.count("scripts/verify_release.py verify-final") == 1


def test_formal_final_verification_runs_after_all_five_evidence_files_land() -> None:
    content, _ = _workflow("release.yml")
    provenance_copy = content.index("github-provenance.sigstore.json")
    sbom_copy = content.index("github-sbom.sigstore.json")
    final_verify = content.index("scripts/verify_release.py verify-final")
    transfer = content.index("name: Transfer signed verified release")

    assert max(provenance_copy, sbom_copy) < final_verify < transfer


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
        "bfd0eb3648273f940882eb0c2ff170b08139b2a9075dc84e260a9232469fa53c"
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
            "017ef46955edcca6a145ee61b3f15cdc0a922c40652009fd725370c4cde9136b"
        ),
        "manifest_sha256": (
            "4ceff416b7d3c6fc5c2bcf03a5039ec25a4089115233898f5e69808dfd5f94d7"
        ),
        "source_count": 3,
        "total_source_bytes": 947504,
    }


def test_all_feature_notices_are_in_root_and_signed_release_policy() -> None:
    features = yaml.safe_load(
        (ROOT / "config" / "features.yaml").read_text(encoding="utf-8")
    )
    policy = json.loads(
        (ROOT / "config" / "release-policy.json").read_text(encoding="utf-8")
    )
    required = set(policy["bundle"]["required_payload_files"])
    root_notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    declared = {
        item["integration"]: item for item in policy["legal"]["feature_notices"]
    }
    expected = {
        item["name"]: item["supply_chain"]["notice_paths"]
        for item in features["integrations"]
        if item["supply_chain"] is not None
    }
    expected_root_ids = {
        "openbb": "OPENBB-AGPL-3.0-SIDECAR",
        "tradingagents": "TRADINGAGENTS-APACHE-2.0-WORKER",
        "kronos": "KRONOS-MIT-WORKER",
        "qlib": "QLIB-MIT-WORKER",
        "nautilus": "NAUTILUS-TRADER-LGPL-3.0-SIDECAR",
        "lean": "QUANTCONNECT-LEAN-APACHE-2.0-SIDECAR",
        "rd_agent": "RD-AGENT-MIT-SANDBOX",
    }

    assert set(declared) == set(expected) == set(expected_root_ids)
    assert "payload/config/features.yaml" in required
    for integration, notice_paths in expected.items():
        item = declared[integration]
        assert item["paths"] == notice_paths
        assert item["root_notice_id"] == expected_root_ids[integration]
        assert item["execution_authority"] is False
        assert root_notices.count(f"## {item['root_notice_id']}\n") == 1
        section = root_notices.split(f"## {item['root_notice_id']}\n", 1)[1]
        section = section.split("\n## ", 1)[0]
        assert "execution authority" in " ".join(section.split())
        for path in notice_paths:
            assert (ROOT / path).is_file()
            assert f"payload/{path}" in required


def test_ai_hedge_fund_full_mit_notice_is_signed_and_copied_into_core_image() -> None:
    relative = "docs/legal/notices/AI-HEDGE-FUND-MIT-PEAD-EVENT-STUDY.md"
    runtime = "/usr/share/licenses/stonks-agent/AI-HEDGE-FUND-MIT-PEAD-EVENT-STUDY.md"
    expected_sha256 = "91607e5dd43d93ad8372921ceacba8a579b07dcd6cd2dd5a2be244d8e6e7696c"
    notice = (ROOT / relative).read_bytes()
    text = notice.decode("utf-8")
    policy = json.loads(
        (ROOT / "config" / "release-policy.json").read_text(encoding="utf-8")
    )
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    root_notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    assert hashlib.sha256(notice).hexdigest() == expected_sha256
    assert "https://github.com/virattt/ai-hedge-fund" in text
    assert "3a18702cb25777fb4bdb4b2527a0c868bc8297f4" in text
    assert "Copyright (c) 2024 Virat Singh" in text
    assert "Permission is hereby granted, free of charge" in text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in text
    root_section = root_notices.split("## AI-HEDGE-FUND-MIT-PEAD-EVENT-STUDY\n", 1)[
        1
    ].split("\n## ", 1)[0]
    assert relative in root_section
    assert f"payload/{relative}" in policy["bundle"]["required_payload_files"]
    assert policy["legal"]["dedicated_notices"] == [
        {
            "id": "AI-HEDGE-FUND-MIT-PEAD-EVENT-STUDY",
            "path": f"payload/{relative}",
            "sha256": expected_sha256,
            "runtime_path": runtime,
        }
    ]
    assert f"COPY --chown=65532:65532 --chmod=0444 {relative} {runtime}" in dockerfile
    allowlist = (
        "docs/*",
        "!docs/legal",
        "docs/legal/*",
        "!docs/legal/notices",
        "docs/legal/notices/*",
        f"!{relative}",
    )
    positions = tuple(dockerignore.index(pattern) for pattern in allowlist)
    assert positions == tuple(sorted(positions))
    assert [
        line
        for line in dockerignore.splitlines()
        if line.startswith("!docs/") and line.endswith(".md")
    ] == [f"!{relative}"]
    for source in (
        ROOT / "src" / "stonks_agent" / "strategies" / "pead.py",
        ROOT / "src" / "stonks_agent" / "analytics" / "event_study.py",
    ):
        content = source.read_text(encoding="utf-8")
        assert "3a18702cb25777fb4bdb4b2527a0c868bc8297f4" in content
        assert "MIT" in content


def test_formal_release_policy_closes_exactly_five_sigstore_evidence_files() -> None:
    policy = json.loads(
        (ROOT / "config" / "release-policy.json").read_text(encoding="utf-8")
    )
    final = policy["signing"]["final_evidence"]

    assert final == {
        "image_bundle": "signatures/core-image.sigstore.json",
        "manifest_bundle": "signatures/release-manifest.sigstore.json",
        "verification_report_bundle": ("signatures/verification-report.sigstore.json"),
        "provenance_bundle": "signatures/github-provenance.sigstore.json",
        "sbom_bundle": "signatures/github-sbom.sigstore.json",
        "provenance_predicate_type": "https://slsa.dev/provenance/v1",
        "sbom_predicate_type": "https://cyclonedx.org/bom",
        "max_bundle_bytes": 33554432,
    }


def test_production_release_policy_stages_every_static_signed_file(
    tmp_path: Path,
) -> None:
    policy = json.loads(
        (ROOT / "config" / "release-policy.json").read_text(encoding="utf-8")
    )
    bundle = tmp_path / "bundle"

    copied = stage_release(ROOT, bundle, policy)

    required = {
        path
        for path in policy["bundle"]["required_payload_files"]
        if not path.startswith("payload/release/")
    }
    staged = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file()
    }
    assert required <= staged
    assert copied == len(staged)
    verify_notices(bundle, policy)


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
