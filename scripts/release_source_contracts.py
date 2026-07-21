"""Fail-closed verification for release corresponding-source archives."""

from __future__ import annotations

import hashlib
import json
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import IO, Any

from scripts.python_source_contract import (
    PythonSourceError,
    verify_source_archive_path,
)
from scripts.release_verifier_common import (
    SHA256_PATTERN,
    ReleaseError,
    as_mapping,
    as_string,
    positive_int,
    regular_status,
    sha256,
    unique_object,
)

ALPINE_SCHEMA = "stonks-agent/alpine-source/v1"
MAX_ALPINE_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ALPINE_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_ALPINE_MEMBERS = 4096
MAX_ALPINE_MEMBER_BYTES = 256 * 1024 * 1024
ALPINE_MANIFEST_KEYS = {
    "file_count",
    "files",
    "origin_count",
    "origins",
    "package_count",
    "package_database_sha256",
    "packages",
    "schema_version",
    "total_source_bytes",
}


def verify_alpine_source(
    archive_path: Path,
    runtime_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify exact legal inventory, canonical archive, and every source member."""
    alpine = as_mapping(runtime_policy.get("alpine"), "core runtime alpine policy")
    source = as_mapping(
        alpine.get("corresponding_source"), "Alpine corresponding source policy"
    )
    _verify_source_decision(source)
    regular_status(archive_path, max_bytes=MAX_ALPINE_ARCHIVE_BYTES)
    expected_archive_hash = _sha_field(source, "archive_sha256")
    if sha256(archive_path) != expected_archive_hash:
        raise ReleaseError("Alpine source archive SHA-256 drifted")
    _verify_gzip_header(archive_path)
    manifest, manifest_hash, members = _read_alpine_archive(archive_path)
    if manifest_hash != _sha_field(source, "manifest_sha256"):
        raise ReleaseError("Alpine source manifest SHA-256 drifted")
    _verify_manifest_identity(manifest, source, alpine)
    _verify_manifest_members(manifest, members)
    result = {
        "archive_sha256": expected_archive_hash,
        "manifest_sha256": _sha_field(source, "manifest_sha256"),
        "package_count": manifest["package_count"],
        "origin_count": manifest["origin_count"],
        "file_count": manifest["file_count"],
        "total_source_bytes": manifest["total_source_bytes"],
    }
    return result


def verify_python_source(
    archive_path: Path,
    policy_path: Path,
    lock_path: Path,
    release_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind the Python source contract result to reviewed release expectations."""
    try:
        summary = verify_source_archive_path(archive_path, policy_path, lock_path)
    except PythonSourceError as error:
        raise ReleaseError("Python corresponding source is invalid") from error
    observed = asdict(summary)
    expected = {
        "archive_sha256": _sha_field(release_policy, "archive_sha256"),
        "manifest_sha256": _sha_field(release_policy, "manifest_sha256"),
        "source_count": positive_int(
            release_policy.get("source_count"), "python_source.source_count"
        ),
        "total_source_bytes": positive_int(
            release_policy.get("total_source_bytes"),
            "python_source.total_source_bytes",
        ),
    }
    if observed != expected:
        raise ReleaseError("Python source summary drifted from release policy")
    return observed


def _verify_source_decision(source: Mapping[str, Any]) -> None:
    if (
        source.get("required_for_distribution") is not True
        or source.get("status") != "verified"
        or source.get("release_decision") != "allow"
    ):
        raise ReleaseError("Alpine corresponding source closure is incomplete")


def _verify_gzip_header(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            header = handle.read(10)
    except OSError as error:
        raise ReleaseError("Alpine source archive cannot be read") from error
    if len(header) != 10 or header[:3] != b"\x1f\x8b\x08" or header[4:8] != b"\0" * 4:
        raise ReleaseError("Alpine source gzip header is not canonical")
    if header[3] & 0x1E:
        raise ReleaseError("Alpine source gzip header contains optional metadata")


def _read_alpine_archive(
    path: Path,
) -> tuple[Mapping[str, Any], str, dict[str, tuple[int, str]]]:
    members: dict[str, tuple[int, str]] = {}
    manifest: Mapping[str, Any] | None = None
    manifest_hash = ""
    seen: set[str] = set()
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            for index, member in enumerate(archive):
                if index >= MAX_ALPINE_MEMBERS:
                    raise ReleaseError(
                        "Alpine source archive member count exceeds policy"
                    )
                _verify_member_metadata(member)
                folded = member.name.casefold()
                if folded in seen:
                    raise ReleaseError("Alpine source archive has duplicate members")
                seen.add(folded)
                handle = archive.extractfile(member)
                if handle is None:
                    raise ReleaseError("Alpine source archive member is unreadable")
                if index == 0:
                    if member.name != "manifest.json":
                        raise ReleaseError("Alpine source manifest must be first")
                    manifest, manifest_hash = _parse_manifest(handle, member.size)
                else:
                    members[member.name] = (member.size, _stream_sha256(handle))
    except ReleaseError:
        raise
    except (OSError, EOFError, tarfile.TarError) as error:
        raise ReleaseError("Alpine source archive is invalid") from error
    if manifest is None:
        raise ReleaseError("Alpine source manifest is missing")
    return manifest, manifest_hash, members


def _verify_member_metadata(member: tarfile.TarInfo) -> None:
    path = PurePosixPath(member.name)
    if (
        not member.isreg()
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or member.size < 0
        or member.size > MAX_ALPINE_MEMBER_BYTES
        or member.uid != 0
        or member.gid != 0
        or member.uname != ""
        or member.gname != ""
        or member.mtime != 0
        or member.mode != 0o644
    ):
        raise ReleaseError("Alpine source archive metadata is not canonical")


def _parse_manifest(handle: IO[bytes], size: int) -> tuple[Mapping[str, Any], str]:
    if size < 2 or size > MAX_ALPINE_MANIFEST_BYTES:
        raise ReleaseError("Alpine source manifest size is outside policy")
    body = handle.read(size + 1)
    if len(body) != size:
        raise ReleaseError("Alpine source manifest size drifted")
    try:
        payload = json.loads(body, object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseError("Alpine source manifest is invalid") from error
    if not isinstance(payload, Mapping) or set(payload) != ALPINE_MANIFEST_KEYS:
        raise ReleaseError("Alpine source manifest fields are invalid")
    canonical = (
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    if body != canonical:
        raise ReleaseError("Alpine source manifest is not canonical")
    return payload, hashlib.sha256(body).hexdigest()


def _verify_manifest_identity(
    manifest: Mapping[str, Any],
    source: Mapping[str, Any],
    alpine: Mapping[str, Any],
) -> None:
    if manifest.get("schema_version") != ALPINE_SCHEMA:
        raise ReleaseError("Alpine source manifest schema is invalid")
    expected_packages = alpine.get("packages")
    if (
        not isinstance(expected_packages, list)
        or manifest.get("packages") != expected_packages
    ):
        raise ReleaseError("Alpine source package inventory drifted")
    expected = {
        "package_database_sha256": _sha_field(source, "package_database_sha256"),
        "package_count": positive_int(source.get("package_count"), "package_count"),
        "origin_count": positive_int(source.get("origin_count"), "origin_count"),
        "file_count": positive_int(source.get("file_count"), "file_count"),
        "total_source_bytes": positive_int(
            source.get("total_source_bytes"), "total_source_bytes"
        ),
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ReleaseError("Alpine source manifest summary drifted")
    _verify_origins(manifest, source, expected_packages)


def _verify_origins(
    manifest: Mapping[str, Any],
    source: Mapping[str, Any],
    packages: Sequence[object],
) -> None:
    commits: dict[str, str] = {}
    for raw in packages:
        package = as_mapping(raw, "Alpine package")
        origin = as_string(package.get("origin"), "Alpine package origin")
        commit = _sha1_field(package, "aports_commit")
        if origin in commits and commits[origin] != commit:
            raise ReleaseError("Alpine origin maps to multiple commits")
        commits[origin] = commit
    paths = as_mapping(source.get("origin_paths"), "Alpine origin paths")
    expected = [
        {
            "origin": origin,
            "aports_commit": commits[origin],
            "aports_path": paths.get(origin),
        }
        for origin in sorted(commits)
    ]
    if manifest.get("origins") != expected or not all(
        isinstance(item["aports_path"], str) for item in expected
    ):
        raise ReleaseError("Alpine source origin provenance drifted")


def _verify_manifest_members(
    manifest: Mapping[str, Any], members: Mapping[str, tuple[int, str]]
) -> None:
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or len(raw_files) != manifest.get("file_count"):
        raise ReleaseError("Alpine source file inventory is invalid")
    expected: dict[str, tuple[int, str]] = {}
    total = 0
    ordered_paths: list[str] = []
    for raw in raw_files:
        item = as_mapping(raw, "Alpine source file")
        if set(item) != {"path", "role", "sha256", "size"}:
            raise ReleaseError("Alpine source file fields are invalid")
        path = as_string(item.get("path"), "Alpine source file path")
        role = item.get("role")
        size = positive_int(item.get("size"), "Alpine source file size")
        digest = _sha_field(item, "sha256")
        if role not in {"recipe", "distfiles"} or path in expected:
            raise ReleaseError("Alpine source file inventory is invalid")
        expected[path] = (size, digest)
        ordered_paths.append(path)
        total += size
    if ordered_paths != sorted(ordered_paths) or expected != members:
        raise ReleaseError("Alpine source archive members drifted")
    if total != manifest.get("total_source_bytes"):
        raise ReleaseError("Alpine source total size drifted")


def _stream_sha256(handle: IO[bytes]) -> str:
    digest = hashlib.sha256()
    while chunk := handle.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _sha_field(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ReleaseError(f"{key} must be an exact SHA-256")
    return value


def _sha1_field(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or len(value) != 40:
        raise ReleaseError(f"{key} must be an exact commit")
    try:
        int(value, 16)
    except ValueError as error:
        raise ReleaseError(f"{key} must be an exact commit") from error
    return value
