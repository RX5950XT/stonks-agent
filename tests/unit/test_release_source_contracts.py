from __future__ import annotations

# ruff: noqa: E402, I001

import hashlib
import sys
import tarfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.alpine_source_contract import AlpinePackage, create_source_archive
from scripts.python_source_contract import SourceArchiveSummary
from scripts.release_source_contracts import (
    ReleaseError,
    verify_alpine_source,
    verify_python_source,
)


def _alpine_fixture(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    staging = tmp_path / "staging"
    recipe = staging / "zlib" / "recipe"
    distfiles = staging / "zlib" / "distfiles"
    recipe.mkdir(parents=True)
    distfiles.mkdir()
    (recipe / "APKBUILD").write_bytes(b"source='zlib.tar.gz'\n")
    (distfiles / "zlib.tar.gz").write_bytes(b"verified-source")
    package = AlpinePackage(
        name="zlib",
        version="1.3.2-r0",
        license="Zlib",
        origin="zlib",
        aports_commit="a" * 40,
    )
    archive = tmp_path / "alpine.tar.gz"
    summary = create_source_archive(
        packages=(package,),
        staging_root=staging,
        output=archive,
        package_database_sha256="b" * 64,
        origin_paths={"zlib": "main/zlib"},
    )
    with tarfile.open(archive, "r:gz") as source:
        handle = source.extractfile("manifest.json")
        assert handle is not None
        manifest_sha256 = hashlib.sha256(handle.read()).hexdigest()
    package_payload = asdict(package)
    runtime_policy: dict[str, Any] = {
        "alpine": {
            "packages": [package_payload],
            "corresponding_source": {
                "required_for_distribution": True,
                "status": "verified",
                "release_decision": "allow",
                "archive_sha256": summary.archive_sha256,
                "manifest_sha256": manifest_sha256,
                "package_database_sha256": "b" * 64,
                "package_count": 1,
                "origin_count": 1,
                "file_count": summary.file_count,
                "total_source_bytes": summary.total_source_bytes,
                "origin_paths": {"zlib": "main/zlib"},
            },
        }
    }
    return archive, runtime_policy


def test_alpine_source_verifies_closed_manifest_and_exact_legal_policy(
    tmp_path: Path,
) -> None:
    archive, policy = _alpine_fixture(tmp_path)

    summary = verify_alpine_source(archive, policy)

    assert summary["package_count"] == 1
    assert summary["origin_count"] == 1
    assert summary["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()

    policy["alpine"]["packages"][0]["version"] = "drifted"
    with pytest.raises(ReleaseError, match="package inventory"):
        verify_alpine_source(archive, policy)


def test_alpine_source_rejects_archive_mutation(tmp_path: Path) -> None:
    archive, policy = _alpine_fixture(tmp_path)
    payload = bytearray(archive.read_bytes())
    payload[-16] ^= 1
    archive.write_bytes(payload)

    with pytest.raises(ReleaseError, match="SHA-256"):
        verify_alpine_source(archive, policy)


def test_python_source_adapter_binds_contract_summary_and_exact_archive_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "python.tar.gz"
    policy_path = tmp_path / "policy.json"
    lock_path = tmp_path / "uv.lock"
    archive.write_bytes(b"archive")
    policy_path.write_text("{}", encoding="utf-8")
    lock_path.write_text("version = 1", encoding="utf-8")
    expected = SourceArchiveSummary(
        archive_sha256=hashlib.sha256(b"archive").hexdigest(),
        manifest_sha256="c" * 64,
        source_count=3,
        total_source_bytes=42,
    )
    monkeypatch.setattr(
        "scripts.release_source_contracts.verify_source_archive_path",
        lambda *_: expected,
    )
    release_policy = {
        "archive_sha256": expected.archive_sha256,
        "manifest_sha256": expected.manifest_sha256,
        "source_count": 3,
        "total_source_bytes": 42,
    }

    assert verify_python_source(
        archive, policy_path, lock_path, release_policy
    ) == asdict(expected)

    release_policy["archive_sha256"] = "d" * 64
    with pytest.raises(ReleaseError, match="Python source summary"):
        verify_python_source(archive, policy_path, lock_path, release_policy)
