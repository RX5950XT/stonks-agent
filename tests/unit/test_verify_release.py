from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

from scripts.release_verifier_bundle import validate_identity

ROOT = Path(__file__).resolve().parents[2]
SPEC = spec_from_file_location(
    "verify_release_under_test",
    ROOT / "scripts" / "verify_release.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

ReleaseError = MODULE.ReleaseError
create_manifest: Any = MODULE.create_manifest
load_json: Any = MODULE.load_json
verify_grype_report: Any = MODULE.verify_grype_report
verify_grype_database_identity: Any = MODULE.verify_grype_database_identity
verify_openbb_source: Any = MODULE.verify_openbb_source
verify_image_report: Any = MODULE.verify_image_report
verify_release: Any = MODULE.verify_release
verify_sbom: Any = MODULE._verify_sbom

IMAGE = "ghcr.io/acme/stonks-agent@sha256:" + ("a" * 64)
COMMIT = "b" * 40


def _policy() -> dict[str, object]:
    return {
        "schema_version": "stonks-agent/release-policy/v1",
        "bundle": {
            "max_files": 32,
            "max_file_bytes": 1_000_000,
            "max_total_bytes": 2_000_000,
            "required_payload_files": [
                "payload/LICENSE",
                "payload/core.cdx.json",
                "payload/core.grype.json",
                "payload/openbb-source.tar.gz",
                "payload/secrets.json",
                "payload/upstream.json",
                "payload/uv.lock",
            ],
        },
        "reports": {
            "secret": "payload/secrets.json",
            "upstream": "payload/upstream.json",
            "grype": "payload/core.grype.json",
        },
        "sbom": {
            "path": "payload/core.cdx.json",
            "inventory_path": "payload/core.inventory.json",
        },
        "openbb": {
            "archive": "payload/openbb-source.tar.gz",
            "max_members": 16,
            "max_member_bytes": 100_000,
            "max_expanded_bytes": 200_000,
            "required_members": {
                "SOURCE_OFFER.md": None,
                "OPENBB_LICENSE.txt": None,
                "uv.lock": None,
                "upstream/openbb_core-1.0.0.tar.gz": "source-sha",
            },
        },
        "signing": {
            "issuer": "https://token.actions.githubusercontent.com",
            "workflow": ".github/workflows/release.yml",
            "image_bundle": "signatures/core-image.sigstore.json",
            "manifest_bundle": "signatures/release-manifest.sigstore.json",
        },
    }


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/acme/stonks-agent-evil@sha256:" + ("a" * 64),
        "ghcr.io/acme/stonks-agent/child@sha256:" + ("a" * 64),
    ],
)
def test_release_identity_rejects_non_exact_repository_image(image: str) -> None:
    with pytest.raises(
        ReleaseError,
        match="image repository does not match release repository",
    ):
        validate_identity(
            version="0.1.0",
            tag="v0.1.0",
            repository="acme/stonks-agent",
            commit=COMMIT,
            image=image,
            signing_mode="unsigned-candidate",
        )


def _tar(path: Path, *, unsafe: bool = False) -> None:
    members = {
        "SOURCE_OFFER.md": b"offer",
        "OPENBB_LICENSE.txt": b"license",
        "uv.lock": b"lock",
        "upstream/openbb_core-1.0.0.tar.gz": b"source",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:", format=tarfile.PAX_FORMAT) as archive:
        for name, content in sorted(members.items()):
            info = tarfile.TarInfo(
                "../escape" if unsafe and name == "uv.lock" else name
            )
            info.size = len(content)
            info.mtime = 0
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(content))
    path.write_bytes(gzip.compress(buffer.getvalue(), mtime=0))


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    payload = bundle / "payload"
    payload.mkdir(parents=True)
    (payload / "LICENSE").write_text("Apache-2.0", encoding="utf-8")
    (payload / "uv.lock").write_text("version = 1", encoding="utf-8")
    (payload / "secrets.json").write_text(
        json.dumps({"success": True, "data": {"finding_count": 0}}),
        encoding="utf-8",
    )
    (payload / "upstream.json").write_text(
        json.dumps({"success": True, "data": {"violation_count": 0}}),
        encoding="utf-8",
    )
    (payload / "core.cdx.json").write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "serialNumber": "urn:uuid:87ad2edb-6a1e-5aee-bb8a-ee169762e3ab",
                "metadata": {
                    "component": {
                        "name": "ghcr.io/acme/stonks-agent",
                        "version": "sha256:" + ("a" * 64),
                    }
                },
                "components": [
                    {
                        "type": "library",
                        "name": "stonks-agent",
                        "version": "0.1.0",
                        "purl": "pkg:pypi/stonks-agent@0.1.0",
                        "licenses": [{"license": {"id": "Apache-2.0"}}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    packages = [
        {
            "type": "library",
            "name": "stonks-agent",
            "version": "0.1.0",
            "purl": "pkg:pypi/stonks-agent@0.1.0",
            "licenses": ["Apache-2.0"],
            "license_source": "sbom",
        }
    ]
    package_bytes = (
        json.dumps(packages, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    (payload / "core.inventory.json").write_text(
        json.dumps(
            {
                "schema_version": "stonks-agent/sbom-inventory/v1",
                "image_reference": IMAGE,
                "component_count": 1,
                "components_sha256": hashlib.sha256(package_bytes).hexdigest(),
                "components": packages,
            }
        ),
        encoding="utf-8",
    )
    (payload / "core.grype.json").write_text(
        json.dumps({"matches": [], "descriptor": {"db": {"built": "2026-07-18"}}}),
        encoding="utf-8",
    )
    _tar(payload / "openbb-source.tar.gz")
    return bundle


def test_release_sbom_requires_image_bound_deterministic_serial(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    policy = _policy()
    sbom_path = bundle / "payload" / "core.cdx.json"

    verify_sbom(bundle, policy, image=IMAGE)

    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    sbom["serialNumber"] = "urn:uuid:11111111-1111-4111-8111-111111111111"
    sbom_path.write_text(json.dumps(sbom), encoding="utf-8")
    with pytest.raises(ReleaseError, match="serialNumber"):
        verify_sbom(bundle, policy, image=IMAGE)

    sbom.pop("serialNumber")
    sbom_path.write_text(json.dumps(sbom), encoding="utf-8")
    with pytest.raises(ReleaseError, match="serialNumber"):
        verify_sbom(bundle, policy, image=IMAGE)


def test_json_loader_rejects_duplicate_keys_and_non_finite_numbers(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"success":true,"success":false}', encoding="utf-8")
    with pytest.raises(ReleaseError, match="duplicate JSON key"):
        load_json(duplicate, max_bytes=1024)

    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"value":NaN}', encoding="utf-8")
    with pytest.raises(ReleaseError, match="non-finite"):
        load_json(invalid, max_bytes=1024)


def test_grype_blocks_high_critical_empty_or_malformed_reports() -> None:
    verify_grype_report({"matches": [], "descriptor": {"db": {"built": "today"}}})

    for severity in ("High", "Critical"):
        with pytest.raises(ReleaseError, match=severity):
            verify_grype_report(
                {
                    "matches": [
                        {
                            "vulnerability": {
                                "id": "CVE-2099-0001",
                                "severity": severity,
                            },
                            "artifact": {"name": "dependency", "version": "1"},
                        }
                    ],
                    "descriptor": {"db": {"built": "today"}},
                }
            )
    with pytest.raises(ReleaseError, match="database identity"):
        verify_grype_report({"matches": []})


def test_grype_accepts_only_exact_non_stale_vex_suppressions() -> None:
    ignored = {
        "matches": [],
        "ignoredMatches": [
            {
                "vulnerability": {
                    "id": "CVE-2099-0001",
                    "severity": "High",
                },
                "artifact": {
                    "name": "python",
                    "version": "3.12.13",
                    "purl": "pkg:generic/python@3.12.13",
                },
                "appliedIgnoreRules": [
                    {"namespace": "vex", "vex-status": "not_affected"}
                ],
            }
        ],
        "descriptor": {"db": {"built": "today"}},
    }
    approved = {
        "CVE-2099-0001": (
            "pkg:generic/python@3.12.13",
            "vulnerable_code_not_present",
        )
    }

    verify_grype_report(ignored, approved)

    with pytest.raises(ReleaseError, match="unreviewed suppressed"):
        verify_grype_report(ignored)
    with pytest.raises(ReleaseError, match="stale VEX"):
        verify_grype_report(
            {"matches": [], "ignoredMatches": [], "descriptor": {"db": {"built": "x"}}},
            approved,
        )


def test_grype_database_status_must_match_the_scan_descriptor_exactly() -> None:
    identity = {
        "schemaVersion": "v6.1.9",
        "from": (
            "https://grype.anchore.io/databases/v6/"
            "vulnerability-db.tar.zst?checksum=sha256%3A" + ("a" * 64)
        ),
        "built": "2026-07-18T06:48:35Z",
        "path": "/grype-db/6/vulnerability.db",
        "valid": True,
    }
    report = {"descriptor": {"db": {"status": identity}}, "matches": []}

    verify_grype_database_identity(report, identity)

    drifted = {**identity, "built": "2026-07-18T06:48:36Z"}
    with pytest.raises(ReleaseError, match="does not match"):
        verify_grype_database_identity(report, drifted)
    with pytest.raises(ReleaseError, match="invalid"):
        verify_grype_database_identity(report, {**identity, "valid": False})


def test_core_image_report_must_bind_the_trusted_release_identity() -> None:
    payload = {
        "schema_version": "stonks-agent/core-image/v1",
        "subject": IMAGE,
        "digest": "sha256:" + ("a" * 64),
        "config_digest": "sha256:" + ("c" * 64),
        "repository": "acme/stonks-agent",
        "revision": COMMIT,
        "version": "0.1.0",
        "source": "https://github.com/acme/stonks-agent",
        "licenses": "Apache-2.0",
        "user": "65532:65532",
        "execution_mode": "paper",
        "registry_verified": True,
    }

    verify_image_report(
        payload,
        image=IMAGE,
        repository="acme/stonks-agent",
        commit=COMMIT,
        version="0.1.0",
        require_registry_identity=True,
    )

    with pytest.raises(ReleaseError, match="identity drifted"):
        verify_image_report(
            {**payload, "revision": "d" * 40},
            image=IMAGE,
            repository="acme/stonks-agent",
            commit=COMMIT,
            version="0.1.0",
            require_registry_identity=True,
        )
    with pytest.raises(ReleaseError, match="closed"):
        verify_image_report(
            {**payload, "unexpected": True},
            image=IMAGE,
            repository="acme/stonks-agent",
            commit=COMMIT,
            version="0.1.0",
            require_registry_identity=True,
        )
    with pytest.raises(ReleaseError, match="registry identity"):
        verify_image_report(
            {**payload, "registry_verified": False},
            image=IMAGE,
            repository="acme/stonks-agent",
            commit=COMMIT,
            version="0.1.0",
            require_registry_identity=True,
        )


def test_openbb_source_rejects_traversal_non_regular_and_bomb(
    tmp_path: Path,
) -> None:
    policy = _policy()["openbb"]
    assert isinstance(policy, dict)
    valid = tmp_path / "valid.tar.gz"
    _tar(valid)
    verify_openbb_source(valid, policy)

    unsafe = tmp_path / "unsafe.tar.gz"
    _tar(unsafe, unsafe=True)
    with pytest.raises(ReleaseError, match="unsafe archive member"):
        verify_openbb_source(unsafe, policy)

    link = tmp_path / "link.tar.gz"
    link_buffer = io.BytesIO()
    with tarfile.open(fileobj=link_buffer, mode="w:") as archive:
        info = tarfile.TarInfo("SOURCE_OFFER.md")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../secret"
        archive.addfile(info)
    link.write_bytes(gzip.compress(link_buffer.getvalue(), mtime=0))
    with pytest.raises(ReleaseError, match="regular files"):
        verify_openbb_source(link, policy)


def test_openbb_source_rejects_nondeterministic_archive_metadata(
    tmp_path: Path,
) -> None:
    policy = _policy()["openbb"]
    assert isinstance(policy, dict)
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w:") as archive:
        info = tarfile.TarInfo("SOURCE_OFFER.md")
        info.size = 5
        info.mtime = 1
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(b"offer"))
    invalid = tmp_path / "nondeterministic.tar.gz"
    invalid.write_bytes(gzip.compress(tar_buffer.getvalue(), mtime=0))

    with pytest.raises(ReleaseError, match="nondeterministic metadata"):
        verify_openbb_source(invalid, policy)

    timestamped = tmp_path / "timestamped.tar.gz"
    timestamped.write_bytes(gzip.compress(tar_buffer.getvalue(), mtime=1))
    with pytest.raises(ReleaseError, match="gzip header"):
        verify_openbb_source(timestamped, policy)


def test_manifest_and_verifier_fail_closed_on_mutation_extra_and_unsigned_release(
    tmp_path: Path,
) -> None:
    policy = _policy()
    bundle = _bundle(tmp_path)
    manifest = create_manifest(
        bundle,
        policy,
        version="0.1.0",
        tag="v0.1.0",
        repository="acme/stonks-agent",
        commit=COMMIT,
        image=IMAGE,
        signing_mode="unsigned-candidate",
    )
    (bundle / "release-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )

    report = verify_release(
        bundle,
        policy,
        expected_repository="acme/stonks-agent",
        expected_tag="v0.1.0",
        expected_commit=COMMIT,
        require_signatures=False,
    )
    assert report["success"] is True
    assert report["signatures_verified"] is False

    (bundle / "payload" / "LICENSE").write_text("changed", encoding="utf-8")
    with pytest.raises(ReleaseError, match="hash or size drift") as raised:
        verify_release(
            bundle,
            policy,
            expected_repository="acme/stonks-agent",
            expected_tag="v0.1.0",
            expected_commit=COMMIT,
            require_signatures=False,
        )
    assert raised.value.phase == "payload_inventory"

    (bundle / "payload" / "LICENSE").write_text("Apache-2.0", encoding="utf-8")
    (bundle / "payload" / "extra").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ReleaseError, match="unexpected payload file"):
        verify_release(
            bundle,
            policy,
            expected_repository="acme/stonks-agent",
            expected_tag="v0.1.0",
            expected_commit=COMMIT,
            require_signatures=False,
        )

    (bundle / "payload" / "extra").unlink()
    with pytest.raises(ReleaseError, match="keyless-release"):
        verify_release(
            bundle,
            policy,
            expected_repository="acme/stonks-agent",
            expected_tag="v0.1.0",
            expected_commit=COMMIT,
            require_signatures=True,
        )


def test_manifest_rejects_symlink_case_collision_and_mutable_image(
    tmp_path: Path,
) -> None:
    policy = _policy()
    bundle = _bundle(tmp_path)
    if os.path.normcase("LICENSE") != os.path.normcase("license"):
        (bundle / "payload" / "license").write_text("collision", encoding="utf-8")
        with pytest.raises(ReleaseError, match="case-colliding"):
            create_manifest(
                bundle,
                policy,
                version="0.1.0",
                tag="v0.1.0",
                repository="acme/stonks-agent",
                commit=COMMIT,
                image=IMAGE,
                signing_mode="unsigned-candidate",
            )
        (bundle / "payload" / "license").unlink()
    target = bundle / "payload" / "LICENSE"
    linked = bundle / "payload" / "linked"
    try:
        linked.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ReleaseError, match="regular file"):
        create_manifest(
            bundle,
            policy,
            version="0.1.0",
            tag="v0.1.0",
            repository="acme/stonks-agent",
            commit=COMMIT,
            image=IMAGE,
            signing_mode="unsigned-candidate",
        )

    linked.unlink()
    with pytest.raises(ReleaseError, match="exact registry image digest"):
        create_manifest(
            bundle,
            policy,
            version="0.1.0",
            tag="v0.1.0",
            repository="acme/stonks-agent",
            commit=COMMIT,
            image="ghcr.io/acme/stonks-agent:latest",
            signing_mode="unsigned-candidate",
        )
