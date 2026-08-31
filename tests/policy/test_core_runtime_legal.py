from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "config" / "release" / "core-runtime-legal.json"
NOTICE_PATH = (
    ROOT
    / "docs"
    / "legal"
    / "notices"
    / "CPYTHON-PYTHON-2.0-COOKIE-SECURITY-BACKPORT.md"
)
ALPINE_NOTICE_PATH = ROOT / "docs" / "legal" / "notices" / "ALPINE-3.23-CORE-RUNTIME.md"
BASE_IMAGE = (
    "python:3.12.13-alpine3.23"
    "@sha256:601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d"
)


def test_linux_release_uses_source_buildable_psycopg_and_system_libpq() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = set(project["project"]["dependencies"])
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "psycopg[c]>=3.3.2,<4; sys_platform == 'linux'" in dependencies
    assert "psycopg[binary]>=3.3.2,<4; sys_platform != 'linux'" in dependencies
    assert "psycopg[binary]>=3.3.2,<4" not in dependencies
    assert "build-base=0.5-r3" in dockerfile
    assert "postgresql18-dev=18.6-r0" in dockerfile
    assert "libpq=18.6-r0" in dockerfile


def _policy() -> dict[str, object]:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def test_cpython_runtime_license_and_backport_provenance_are_exact() -> None:
    policy = _policy()
    assert policy["schema_version"] == "stonks-agent/core-runtime-legal/v1"
    assert policy["base_image"] == BASE_IMAGE

    python = policy["python"]
    assert isinstance(python, dict)
    assert python == {
        "component": "CPython",
        "version": "3.12.13",
        "overall_license_expression": "Python-2.0",
        "primary_license_expression": "PSF-2.0",
        "runtime_license_path": "/usr/local/lib/python3.12/LICENSE.txt",
        "runtime_license_sha256": (
            "3b2f81fe21d181c499c59a256c8e1968455d6689d269aa85373bfb6af41da3bf"
        ),
        "release_source_url": "https://github.com/python/cpython/tree/v3.12.13",
        "release_license_url": (
            "https://github.com/python/cpython/blob/v3.12.13/LICENSE"
        ),
    }

    backport = policy["security_backport"]
    assert isinstance(backport, dict)
    assert backport["cve"] == "CVE-2026-3644"
    assert backport["kind"] == "selective-source-backport"
    assert backport["target_path"] == "/usr/local/lib/python3.12/http/cookies.py"
    assert backport["upstream_commit"] == ("57e88c1cf95e1481b94ae57abe1010469d47a6b4")
    assert backport["upstream_commit_url"] == (
        "https://github.com/python/cpython/commit/"
        "57e88c1cf95e1481b94ae57abe1010469d47a6b4"
    )
    assert backport["upstream_license_url"] == (
        "https://github.com/python/cpython/blob/"
        "57e88c1cf95e1481b94ae57abe1010469d47a6b4/LICENSE"
    )
    assert backport["upstream_license_sha256"] == (
        "b0e25a78cffb43f4d92de8b61ccfa1f1f98ecbc22330b54b5251e7b6ba010231"
    )
    assert backport["base_source_sha256"] == (
        "e79e3858e22266a709c3cac3b0c0b14b9a3f074621145d67e1abc01fb6613ae3"
    )
    assert backport["upstream_file_sha256"] == (
        "407579d026cb4ba7bba7952c97e52e8d3a270a92679e896d601cd13a9a06260e"
    )
    assert backport["installed_file_sha256"] == (
        "6387f676095ae5374943eff99fbcd2d9c681172c00209fadf54c311cf7228149"
    )
    assert backport["license_expression"] == "Python-2.0"
    assert backport["official_cpython_release"] is False
    assert backport["whole_file_copy"] is False
    assert backport["changes"] == [
        "Morsel.update control-character rejection",
        "Morsel.__ior__ delegates to validated update",
        "Morsel.__setstate__ control-character rejection",
        "BaseCookie.js_output control-character rejection",
    ]

    patch_script = (ROOT / "scripts" / "patch_cpython_stdlib.py").read_text(
        encoding="utf-8"
    )
    for value in (
        backport["upstream_commit"],
        backport["base_source_sha256"],
        backport["installed_file_sha256"],
    ):
        assert isinstance(value, str)
        assert value in patch_script


def test_alpine_package_snapshot_is_exact_and_source_policy_is_bounded() -> None:
    policy = _policy()
    alpine = policy["alpine"]
    assert isinstance(alpine, dict)
    assert alpine["release"] == "3.23"
    assert alpine["platform"] == "linux/amd64"
    assert alpine["package_database_path"] == "/lib/apk/db/installed"

    packages = alpine["packages"]
    assert isinstance(packages, list)
    assert len(packages) == 37
    assert [item["name"] for item in packages] == sorted(
        item["name"] for item in packages
    )
    assert len({item["name"] for item in packages}) == len(packages)
    for item in packages:
        assert set(item) == {
            "aports_commit",
            "license",
            "name",
            "origin",
            "version",
        }
        assert re.fullmatch(r"[0-9a-f]{40}", item["aports_commit"])
        assert all(item[key] for key in ("license", "name", "origin", "version"))
    assert _canonical_sha256(packages) == alpine["package_inventory_sha256"]
    assert alpine["package_inventory_sha256"] == (
        "ccecb09689f65955f2402a9d6851bd38a7df8bf5aad9f91433259d0d1842d897"
    )
    assert next(item for item in packages if item["name"] == "libpq") == {
        "aports_commit": "c2ee21f8f682d22ae282d0b82b2427a2df335548",
        "license": "PostgreSQL",
        "name": "libpq",
        "origin": "postgresql18",
        "version": "18.6-r0",
    }

    source = alpine["corresponding_source"]
    assert isinstance(source, dict)
    assert source["required_for_distribution"] is True
    assert source["required_archive_path"] == (
        "payload/release/alpine-corresponding-source.tar.gz"
    )
    assert source["sandbox_image"].startswith("docker.io/library/alpine:3.23@sha256:")
    assert source["origin_paths"]["libnsl"] == "community/libnsl"
    assert source["origin_paths"]["postgresql18"] == "main/postgresql18"
    assert source["status"] == "verified"
    assert source["release_decision"] == "allow"
    assert source["archive_sha256"] == (
        "88ee68944eb4204033d7caf269b530245509538288173cf6cc9b517467cde0d3"
    )
    assert source["manifest_sha256"] == (
        "8e453b818b5357d8cf91aa27fc3f149e5840f256564d8de373c9b5b9f90ca470"
    )
    assert source["package_database_sha256"] == (
        "789fbae58431cf5c0b0354c11889ee834d98da64835db3179455b7867b63a674"
    )
    assert source["package_count"] == 37
    assert source["origin_count"] == 27
    assert source["file_count"] == 244
    assert source["total_source_bytes"] == 133_140_072
    assert any("GPL-" in item["license"] for item in packages)
    assert any("LGPL-" in item["license"] for item in packages)
    assert any("MPL-" in item["license"] for item in packages)


def test_release_bundle_and_notices_bind_runtime_legal_policy() -> None:
    release = json.loads(
        (ROOT / "config" / "release-policy.json").read_text(encoding="utf-8")
    )
    required = set(release["bundle"]["required_payload_files"])
    assert {
        "payload/config/release/core-runtime-legal.json",
        ("payload/docs/legal/notices/CPYTHON-PYTHON-2.0-COOKIE-SECURITY-BACKPORT.md"),
        "payload/docs/legal/notices/ALPINE-3.23-CORE-RUNTIME.md",
    } <= required
    assert release["legal"]["core_runtime_policy_path"] == (
        "payload/config/release/core-runtime-legal.json"
    )
    assert {
        "CPYTHON-PYTHON-2.0-COOKIE-SECURITY-BACKPORT",
        "ALPINE-3.23-CORE-RUNTIME",
    } <= set(release["legal"]["required_notice_ids"])

    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "目前 Stonks Agent core 沒有複製、修改" not in notices
    for notice_id in (
        "CPYTHON-PYTHON-2.0-COOKIE-SECURITY-BACKPORT",
        "ALPINE-3.23-CORE-RUNTIME",
    ):
        assert notice_id in notices


def test_notices_describe_the_backport_and_alpine_blocker_without_overclaim() -> None:
    notice = NOTICE_PATH.read_text(encoding="utf-8")
    for token in (
        "Python-2.0",
        "PSF-2.0",
        "/usr/local/lib/python3.12/LICENSE.txt",
        "57e88c1cf95e1481b94ae57abe1010469d47a6b4",
        "e79e3858e22266a709c3cac3b0c0b14b9a3f074621145d67e1abc01fb6613ae3",
        "407579d026cb4ba7bba7952c97e52e8d3a270a92679e896d601cd13a9a06260e",
        "6387f676095ae5374943eff99fbcd2d9c681172c00209fadf54c311cf7228149",
        "b0e25a78cffb43f4d92de8b61ccfa1f1f98ecbc22330b54b5251e7b6ba010231",
        "selective backport",
        "不是 CPython 官方 3.12.x release",
    ):
        assert token in notice

    alpine_notice = ALPINE_NOTICE_PATH.read_text(encoding="utf-8")
    for token in (
        "37",
        "/lib/apk/db/installed",
        "GPL",
        "LGPL",
        "MPL",
        "ccecb09689f65955f2402a9d6851bd38a7df8bf5aad9f91433259d0d1842d897",
        "88ee68944eb4204033d7caf269b530245509538288173cf6cc9b517467cde0d3",
        "8e453b818b5357d8cf91aa27fc3f149e5840f256564d8de373c9b5b9f90ca470",
        "alpine-corresponding-source.tar.gz",
        "已驗證的 corresponding source",
        "任何 drift 都 fail closed",
    ):
        assert token in alpine_notice
