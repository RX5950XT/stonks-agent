#!/usr/bin/env python3
"""Validate the exact deterministic Python corresponding-source archive."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import tarfile
import tomllib
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

POLICY_SCHEMA = "stonks-agent/python-source-policy/v1"
ARCHIVE_SCHEMA = "stonks-agent/python-source/v1"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PACKAGE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}$")
FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}\.tar\.gz$")
MAX_POLICY_BYTES = 64 * 1024
MAX_LOCK_BYTES = 16 * 1024 * 1024
MAX_PACKAGES = 16
EXPECTED_HOSTS = frozenset({"files.pythonhosted.org"})
EXPECTED_PACKAGES = (
    ("certifi", "2026.6.17"),
    ("psycopg", "3.3.4"),
    ("psycopg-c", "3.3.4"),
)
POLICY_KEYS = {
    "allowed_hosts",
    "max_archive_bytes",
    "max_source_bytes",
    "max_total_source_bytes",
    "packages",
    "schema_version",
}


class PythonSourceError(ValueError):
    """Raised when Python source evidence is not closed and trustworthy."""


@dataclass(frozen=True, slots=True, order=True)
class LockedSource:
    name: str
    version: str
    filename: str
    url: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class SourcePlan:
    allowed_hosts: frozenset[str]
    max_archive_bytes: int
    max_source_bytes: int
    max_total_source_bytes: int
    policy_sha256: str
    lock_sha256: str
    sources: tuple[LockedSource, ...]


@dataclass(frozen=True, slots=True)
class SourceArchiveSummary:
    archive_sha256: str
    manifest_sha256: str
    source_count: int
    total_source_bytes: int


def load_source_plan(policy_path: Path, lock_path: Path) -> SourcePlan:
    policy_bytes = _read_bounded(policy_path, MAX_POLICY_BYTES, "source policy")
    lock_bytes = _read_bounded(lock_path, MAX_LOCK_BYTES, "uv.lock")
    policy = _load_json(policy_bytes)
    lock = _load_toml(lock_bytes)
    hosts = _allowed_hosts(policy)
    max_source = _positive_int(policy, "max_source_bytes")
    max_total = _positive_int(policy, "max_total_source_bytes")
    max_archive = _positive_int(policy, "max_archive_bytes")
    requested = _requested_packages(policy)
    sources = _locked_sources(lock, requested, hosts, max_source)
    total = sum(source.size for source in sources)
    if total > max_total:
        raise PythonSourceError("locked Python source total exceeds policy")
    if max_archive <= total or max_archive > 64 * 1024 * 1024:
        raise PythonSourceError("Python source archive bound is invalid")
    return SourcePlan(
        allowed_hosts=hosts,
        max_archive_bytes=max_archive,
        max_source_bytes=max_source,
        max_total_source_bytes=max_total,
        policy_sha256=hashlib.sha256(policy_bytes).hexdigest(),
        lock_sha256=hashlib.sha256(lock_bytes).hexdigest(),
        sources=sources,
    )


def validate_download_url(url: str, allowed_hosts: frozenset[str]) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        raise PythonSourceError("Python source URL must use an approved host")
    if not parsed.path.startswith("/packages/"):
        raise PythonSourceError("Python source URL path is invalid")


def build_archive_bytes(
    plan: SourcePlan,
    payloads: Mapping[str, bytes],
) -> bytes:
    expected = {source.filename for source in plan.sources}
    if set(payloads) != expected:
        raise PythonSourceError("Python source payload set is not closed")
    for source in plan.sources:
        _validate_payload(source, payloads[source.filename])
    manifest = _manifest_bytes(plan)
    members = {"manifest.json": manifest}
    members.update(
        {
            f"sources/{source.filename}": payloads[source.filename]
            for source in plan.sources
        }
    )
    uncompressed = io.BytesIO()
    with tarfile.open(
        fileobj=uncompressed, mode="w", format=tarfile.USTAR_FORMAT
    ) as tar:
        for name in sorted(members):
            _add_bytes(tar, name, members[name])
    compressed = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", fileobj=compressed, compresslevel=9, mtime=0
    ) as stream:
        stream.write(uncompressed.getvalue())
    result = compressed.getvalue()
    if len(result) > plan.max_archive_bytes:
        raise PythonSourceError("Python source archive exceeds policy")
    return result


def verify_source_archive(
    archive: bytes,
    plan: SourcePlan,
) -> SourceArchiveSummary:
    if not archive or len(archive) > plan.max_archive_bytes:
        raise PythonSourceError("Python source archive exceeds policy")
    members = _read_archive(
        archive,
        len(plan.sources) + 1,
        max_member_bytes=max(plan.max_source_bytes, MAX_POLICY_BYTES),
        max_total_bytes=plan.max_total_source_bytes + MAX_POLICY_BYTES,
    )
    expected_names = {"manifest.json"} | {
        f"sources/{source.filename}" for source in plan.sources
    }
    if set(members) != expected_names:
        raise PythonSourceError("Python source archive member set is not closed")
    expected_manifest = _manifest_bytes(plan)
    if members["manifest.json"] != expected_manifest:
        raise PythonSourceError("Python source manifest does not match policy and lock")
    payloads = {
        source.filename: members[f"sources/{source.filename}"]
        for source in plan.sources
    }
    canonical = build_archive_bytes(plan, payloads)
    if archive != canonical:
        raise PythonSourceError("Python source archive is not canonical")
    return SourceArchiveSummary(
        archive_sha256=hashlib.sha256(archive).hexdigest(),
        manifest_sha256=hashlib.sha256(expected_manifest).hexdigest(),
        source_count=len(plan.sources),
        total_source_bytes=sum(source.size for source in plan.sources),
    )


def verify_source_archive_path(
    archive_path: Path,
    policy_path: Path,
    lock_path: Path,
) -> SourceArchiveSummary:
    plan = load_source_plan(policy_path, lock_path)
    archive = _read_bounded(archive_path, plan.max_archive_bytes, "source archive")
    return verify_source_archive(archive, plan)


def _allowed_hosts(policy: Mapping[str, Any]) -> frozenset[str]:
    if policy.get("schema_version") != POLICY_SCHEMA:
        raise PythonSourceError("Python source policy schema is invalid")
    if set(policy) != POLICY_KEYS:
        raise PythonSourceError("Python source policy fields are invalid")
    raw = policy.get("allowed_hosts")
    if not isinstance(raw, list) or not raw or len(raw) > 8:
        raise PythonSourceError("Python source approved hosts are invalid")
    hosts: set[str] = set()
    for value in raw:
        if not isinstance(value, str) or value != value.lower() or "." not in value:
            raise PythonSourceError("Python source approved hosts are invalid")
        hosts.add(value)
    if len(hosts) != len(raw):
        raise PythonSourceError("Python source approved hosts contain duplicates")
    if frozenset(hosts) != EXPECTED_HOSTS:
        raise PythonSourceError("Python source approved hosts drifted")
    return EXPECTED_HOSTS


def _requested_packages(
    policy: Mapping[str, Any],
) -> tuple[tuple[str, str], ...]:
    raw = policy.get("packages")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_PACKAGES:
        raise PythonSourceError("Python source package policy is invalid")
    result: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {"name", "version"}:
            raise PythonSourceError("Python source package policy is invalid")
        name, version = item.get("name"), item.get("version")
        if (
            not isinstance(name, str)
            or not PACKAGE_PATTERN.fullmatch(name)
            or not isinstance(version, str)
            or not VERSION_PATTERN.fullmatch(version)
        ):
            raise PythonSourceError("Python source package policy is invalid")
        result.append((name, version))
    if tuple(result) != EXPECTED_PACKAGES:
        raise PythonSourceError("Python source package policy drifted")
    return tuple(result)


def _locked_sources(
    lock: Mapping[str, Any],
    requested: Sequence[tuple[str, str]],
    allowed_hosts: frozenset[str],
    max_source_bytes: int,
) -> tuple[LockedSource, ...]:
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise PythonSourceError("uv.lock package table is invalid")
    indexed: dict[tuple[str, str], Mapping[str, Any]] = {}
    for raw in packages:
        if not isinstance(raw, Mapping):
            raise PythonSourceError("uv.lock package table is invalid")
        name, version = raw.get("name"), raw.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            continue
        key = (name, version)
        if key in requested:
            if key in indexed:
                raise PythonSourceError("uv.lock contains duplicate requested package")
            indexed[key] = raw
    if set(indexed) != set(requested):
        raise PythonSourceError("uv.lock lacks an exact requested package")
    return tuple(
        _locked_source(indexed[key], key, allowed_hosts, max_source_bytes)
        for key in requested
    )


def _locked_source(
    package: Mapping[str, Any],
    key: tuple[str, str],
    allowed_hosts: frozenset[str],
    max_source_bytes: int,
) -> LockedSource:
    source = package.get("source")
    if (
        not isinstance(source, Mapping)
        or source.get("registry") != "https://pypi.org/simple"
    ):
        raise PythonSourceError("requested package is not locked to PyPI")
    sdist = package.get("sdist")
    if not isinstance(sdist, Mapping):
        raise PythonSourceError("requested package lacks a locked sdist")
    url, digest, size = sdist.get("url"), sdist.get("hash"), sdist.get("size")
    if not isinstance(url, str):
        raise PythonSourceError("locked Python source URL is invalid")
    validate_download_url(url, allowed_hosts)
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise PythonSourceError("locked Python source SHA-256 is invalid")
    sha256 = digest.removeprefix("sha256:")
    if not SHA256_PATTERN.fullmatch(sha256):
        raise PythonSourceError("locked Python source SHA-256 is invalid")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or not 0 < size <= max_source_bytes
    ):
        raise PythonSourceError("locked Python source size exceeds policy")
    filename = PurePosixPath(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
    if not FILENAME_PATTERN.fullmatch(filename):
        raise PythonSourceError("locked Python source filename is invalid")
    return LockedSource(key[0], key[1], filename, url, sha256, size)


def _manifest_bytes(plan: SourcePlan) -> bytes:
    payload = {
        "schema_version": ARCHIVE_SCHEMA,
        "lock_sha256": plan.lock_sha256,
        "policy_sha256": plan.policy_sha256,
        "source_count": len(plan.sources),
        "total_source_bytes": sum(source.size for source in plan.sources),
        "sources": [asdict(source) for source in plan.sources],
    }
    return _json_bytes(payload)


def _read_archive(
    archive: bytes,
    expected_count: int,
    *,
    max_member_bytes: int,
    max_total_bytes: int,
) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            while (member := tar.next()) is not None:
                if len(result) >= expected_count:
                    raise PythonSourceError("Python source archive has excess members")
                _validate_member(member)
                if member.size > max_member_bytes:
                    raise PythonSourceError(
                        "Python source archive member exceeds policy"
                    )
                total += member.size
                if total > max_total_bytes:
                    raise PythonSourceError(
                        "Python source archive total exceeds policy"
                    )
                if member.name in result:
                    raise PythonSourceError(
                        "Python source archive has duplicate members"
                    )
                handle = tar.extractfile(member)
                if handle is None:
                    raise PythonSourceError(
                        "Python source archive member is unreadable"
                    )
                body = handle.read(member.size + 1)
                if len(body) != member.size:
                    raise PythonSourceError("Python source archive member size drifted")
                result[member.name] = body
            if len(result) != expected_count:
                raise PythonSourceError("Python source archive member count is invalid")
    except (OSError, EOFError, tarfile.TarError) as error:
        raise PythonSourceError("Python source archive is invalid") from error
    return result


def _validate_member(member: tarfile.TarInfo) -> None:
    path = PurePosixPath(member.name)
    if (
        not member.isreg()
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or member.uid != 0
        or member.gid != 0
        or member.uname != ""
        or member.gname != ""
        or member.mtime != 0
        or member.mode != 0o644
    ):
        raise PythonSourceError("Python source archive metadata is not canonical")


def _validate_payload(source: LockedSource, payload: bytes) -> None:
    if len(payload) != source.size:
        raise PythonSourceError("Python source payload has the wrong exact size")
    if hashlib.sha256(payload).hexdigest() != source.sha256:
        raise PythonSourceError("Python source payload SHA-256 does not match uv.lock")


def _read_bounded(path: Path, maximum: int, label: str) -> bytes:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
            raise PythonSourceError(f"{label} is not a bounded regular file")
        payload = path.read_bytes()
    except OSError as error:
        raise PythonSourceError(f"{label} cannot be read") from error
    if not payload or len(payload) > maximum:
        raise PythonSourceError(f"{label} is not a bounded regular file")
    return payload


def _load_json(payload: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PythonSourceError("Python source policy JSON is invalid") from error
    if not isinstance(value, Mapping):
        raise PythonSourceError("Python source policy JSON is invalid")
    return value


def _load_toml(payload: bytes) -> Mapping[str, Any]:
    try:
        value = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PythonSourceError("uv.lock TOML is invalid") from error
    return value


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PythonSourceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _positive_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PythonSourceError(f"Python source policy {key} is invalid")
    return value


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _add_bytes(tar: tarfile.TarFile, name: str, body: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(body)
    info.mode = 0o644
    info.uid = info.gid = info.mtime = 0
    info.uname = info.gname = ""
    tar.addfile(info, io.BytesIO(body))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--uv-lock", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        summary = verify_source_archive_path(args.archive, args.policy, args.uv_lock)
        result: dict[str, object] = {
            "success": True,
            "status": "passed",
            "data": asdict(summary),
            "error": None,
        }
    except PythonSourceError:
        result = {
            "success": False,
            "status": "failed",
            "data": None,
            "error": {"code": "PYTHON_SOURCE_INVALID"},
        }
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["success"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
