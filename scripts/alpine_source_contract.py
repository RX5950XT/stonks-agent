"""Pure validation and deterministic archive helpers for Alpine source closure."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

SCHEMA_VERSION = "stonks-agent/alpine-source/v1"
APORTS_API = (
    "https://gitlab.alpinelinux.org/api/v4/projects/1/repository/archive.tar.gz"
)
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]{0,127}$")
SAFE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._~:-]{0,191}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_PACKAGE_DATABASE_BYTES = 16 * 1024 * 1024
MAX_PACKAGES = 512
MAX_ORIGINS = 256
MAX_RECIPE_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_RECIPE_FILES = 512
MAX_RECIPE_FILE_BYTES = 8 * 1024 * 1024
MAX_RECIPE_TOTAL_BYTES = 32 * 1024 * 1024
MAX_BUNDLE_FILES = 4096
MAX_BUNDLE_FILE_BYTES = 512 * 1024 * 1024
MAX_BUNDLE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
APORTS_FETCH_ATTEMPTS = 3
APORTS_FETCH_RETRY_DELAY_SECONDS = 0.25


class AlpineSourceError(ValueError):
    """Raised when Alpine source evidence cannot be trusted."""


@dataclass(frozen=True, slots=True, order=True)
class AlpinePackage:
    name: str
    version: str
    license: str
    origin: str
    aports_commit: str


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SourceArchiveSummary:
    package_count: int
    origin_count: int
    file_count: int
    total_source_bytes: int
    archive_sha256: str


@dataclass(frozen=True, slots=True)
class _BundleEntry:
    path: str
    role: str
    sha256: str
    size: int
    source: Path


def parse_apk_database(payload: str) -> tuple[AlpinePackage, ...]:
    encoded = payload.encode("utf-8")
    if len(encoded) > MAX_PACKAGE_DATABASE_BYTES:
        raise AlpineSourceError("APK package database exceeds policy")
    if "\x00" in payload:
        raise AlpineSourceError("APK package database contains NUL")
    packages: list[AlpinePackage] = []
    seen: set[tuple[str, str]] = set()
    for record_number, record in enumerate(re.split(r"\r?\n\r?\n", payload), 1):
        if not record.strip():
            continue
        package = _package_from_fields(
            _apk_fields(record, record_number), record_number
        )
        key = (package.name, package.version)
        if key in seen:
            raise AlpineSourceError("duplicate package in APK database")
        seen.add(key)
        packages.append(package)
    if not packages:
        raise AlpineSourceError("APK package database is empty")
    if len(packages) > MAX_PACKAGES:
        raise AlpineSourceError("APK package count exceeds policy")
    return tuple(sorted(packages))


def validate_reviewed_inventory(
    packages: Sequence[AlpinePackage],
    reviewed: object,
) -> None:
    if not isinstance(reviewed, list) or not reviewed:
        raise AlpineSourceError("reviewed Alpine package inventory is missing")
    expected: list[AlpinePackage] = []
    for item in reviewed:
        if not isinstance(item, Mapping):
            raise AlpineSourceError("reviewed Alpine package inventory is invalid")
        package = AlpinePackage(
            name=_required_text(item, "name"),
            version=_required_text(item, "version"),
            license=_required_text(item, "license"),
            origin=_required_text(item, "origin"),
            aports_commit=_required_text(item, "aports_commit"),
        )
        _validate_package(package)
        expected.append(package)
    if tuple(sorted(expected)) != tuple(sorted(packages)):
        raise AlpineSourceError(
            "reviewed Alpine inventory does not match image metadata"
        )


def origin_commits(packages: Sequence[AlpinePackage]) -> dict[str, str]:
    result: dict[str, str] = {}
    for package in packages:
        existing = result.setdefault(package.origin, package.aports_commit)
        if existing != package.aports_commit:
            raise AlpineSourceError("one Alpine origin has conflicting aports commits")
    if not result or len(result) > MAX_ORIGINS:
        raise AlpineSourceError("Alpine origin count exceeds policy")
    return dict(sorted(result.items()))


def validated_origin_paths(
    commits: Mapping[str, str],
    values: object,
) -> dict[str, str]:
    if values is None:
        result = {origin: f"main/{origin}" for origin in commits}
    elif isinstance(values, Mapping):
        result = {}
        for key, value in values.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise AlpineSourceError("reviewed aports paths are invalid")
            result[key] = value
    else:
        raise AlpineSourceError("reviewed aports paths are invalid")
    if set(result) != set(commits):
        raise AlpineSourceError("reviewed aports paths do not match image origins")
    for origin, path in result.items():
        _validate_aports_path(origin, path)
    return dict(sorted(result.items()))


def build_aports_archive_url(
    origin: str,
    commit: str,
    *,
    aports_path: str | None = None,
) -> str:
    _validate_origin(origin)
    _validate_commit(commit)
    exact_path = aports_path or f"main/{origin}"
    _validate_aports_path(origin, exact_path)
    query = urllib.parse.urlencode(
        {"sha": commit, "path": exact_path},
        quote_via=urllib.parse.quote,
    )
    return f"{APORTS_API}?{query}"


def fetch_aports_archive(url: str, max_bytes: int) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "gitlab.alpinelinux.org"
        or parsed.path != "/api/v4/projects/1/repository/archive.tar.gz"
    ):
        raise AlpineSourceError("aports request must use the official GitLab API")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/gzip",
            "User-Agent": "stonks-agent-alpine-source/1",
        },
        method="GET",
    )
    last_error: Exception | None = None
    for attempt in range(APORTS_FETCH_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                declared = response.headers.get("Content-Length")
                if declared is not None and int(declared) > max_bytes:
                    raise AlpineSourceError("aports archive exceeds policy")
                payload = bytes(response.read(max_bytes + 1))
        except AlpineSourceError:
            raise
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_error = error
            if attempt + 1 == APORTS_FETCH_ATTEMPTS:
                break
            time.sleep(APORTS_FETCH_RETRY_DELAY_SECONDS * (attempt + 1))
            continue
        if not payload or len(payload) > max_bytes:
            raise AlpineSourceError("aports archive exceeds policy")
        return payload
    raise AlpineSourceError("aports archive download failed") from last_error


def extract_recipe_archive(
    payload: bytes,
    *,
    origin: str,
    commit: str,
    destination: Path,
    aports_path: str | None = None,
) -> tuple[SourceFile, ...]:
    _validate_origin(origin)
    _validate_commit(commit)
    if not payload or len(payload) > MAX_RECIPE_ARCHIVE_BYTES:
        raise AlpineSourceError("aports archive exceeds policy")
    _prepare_empty_directory(destination)
    exact_path = aports_path or f"main/{origin}"
    _validate_aports_path(origin, exact_path)
    section = exact_path.split("/", 1)[0]
    root = f"aports-{commit}-{commit}-{section}-{origin}"
    prefix = PurePosixPath(root, section, origin)
    regular_files, links = _read_recipe_members(payload, prefix)
    for path, target in links.items():
        try:
            regular_files[path] = regular_files[target]
        except KeyError as error:
            raise AlpineSourceError(
                "aports symlink target is not a regular file"
            ) from error
    extracted: list[SourceFile] = []
    for relative, body in sorted(regular_files.items()):
        target_path = destination.joinpath(*relative.parts)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _write_new_file(target_path, body)
        extracted.append(
            SourceFile(
                path=relative.as_posix(),
                size=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
            )
        )
    if not any(item.path == "APKBUILD" for item in extracted):
        raise AlpineSourceError("aports archive does not contain APKBUILD")
    return tuple(extracted)


def create_source_archive(
    *,
    packages: Sequence[AlpinePackage],
    staging_root: Path,
    output: Path,
    package_database_sha256: str,
    origin_paths: Mapping[str, str] | None = None,
) -> SourceArchiveSummary:
    if not SHA256_PATTERN.fullmatch(package_database_sha256):
        raise AlpineSourceError("package database SHA-256 is invalid")
    sorted_packages = tuple(sorted(packages))
    if not sorted_packages or len(sorted_packages) > MAX_PACKAGES:
        raise AlpineSourceError("Alpine package inventory is invalid")
    for package in sorted_packages:
        _validate_package(package)
    commits = origin_commits(sorted_packages)
    reviewed_paths = validated_origin_paths(commits, origin_paths)
    _validate_staging_root(staging_root, frozenset(commits))
    entries = _source_files(staging_root, commits)
    total_bytes = sum(entry.size for entry in entries)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "package_database_sha256": package_database_sha256,
        "package_count": len(sorted_packages),
        "origin_count": len(commits),
        "file_count": len(entries),
        "total_source_bytes": total_bytes,
        "packages": [asdict(package) for package in sorted_packages],
        "origins": [
            {
                "origin": origin,
                "aports_commit": commits[origin],
                "aports_path": reviewed_paths[origin],
            }
            for origin in sorted(commits)
        ],
        "files": [
            {
                "path": entry.path,
                "role": entry.role,
                "sha256": entry.sha256,
                "size": entry.size,
            }
            for entry in entries
        ],
    }
    _write_archive(entries, _canonical_json(manifest), output)
    archive_sha256 = hashlib.sha256(output.read_bytes()).hexdigest()
    return SourceArchiveSummary(
        package_count=len(sorted_packages),
        origin_count=len(commits),
        file_count=len(entries),
        total_source_bytes=total_bytes,
        archive_sha256=archive_sha256,
    )


def _read_recipe_members(
    payload: bytes,
    prefix: PurePosixPath,
) -> tuple[dict[PurePosixPath, bytes], dict[PurePosixPath, PurePosixPath]]:
    allowed_directories = {prefix.parents[1], prefix.parent, prefix}
    seen: set[str] = set()
    regular_files: dict[PurePosixPath, bytes] = {}
    links: dict[PurePosixPath, PurePosixPath] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_RECIPE_FILES:
                raise AlpineSourceError("aports archive member count exceeds policy")
            for member in members:
                member_path = _safe_member_path(member.name)
                folded = member_path.as_posix().casefold()
                if folded in seen:
                    raise AlpineSourceError("duplicate aports archive member")
                seen.add(folded)
                if member.isdir():
                    if member_path not in allowed_directories and not _is_below(
                        member_path, prefix
                    ):
                        raise AlpineSourceError(
                            "aports archive contains an unexpected path"
                        )
                    continue
                if not _is_below(member_path, prefix):
                    raise AlpineSourceError(
                        "aports archive must contain only expected recipe paths"
                    )
                relative = member_path.relative_to(prefix)
                if member.issym():
                    target = _safe_link_target(member_path, member.linkname)
                    if not _is_below(target, prefix):
                        raise AlpineSourceError("aports symlink escapes its recipe")
                    links[relative] = target.relative_to(prefix)
                    continue
                total = _read_regular_member(
                    archive, member, relative, regular_files, total
                )
    except (tarfile.TarError, OSError) as error:
        raise AlpineSourceError("invalid aports archive") from error
    return regular_files, links


def _read_regular_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    relative: PurePosixPath,
    result: dict[PurePosixPath, bytes],
    total: int,
) -> int:
    if not member.isreg():
        raise AlpineSourceError(
            "aports archive must contain only regular files or safe symlinks"
        )
    if member.size > MAX_RECIPE_FILE_BYTES:
        raise AlpineSourceError("aports recipe file exceeds policy")
    next_total = total + member.size
    if next_total > MAX_RECIPE_TOTAL_BYTES:
        raise AlpineSourceError("aports recipe total exceeds policy")
    handle = archive.extractfile(member)
    if handle is None:
        raise AlpineSourceError("aports recipe file cannot be read")
    body = handle.read(MAX_RECIPE_FILE_BYTES + 1)
    if len(body) != member.size:
        raise AlpineSourceError("aports recipe file size is inconsistent")
    result[relative] = body
    return next_total


def _source_files(
    staging_root: Path,
    commits: Mapping[str, str],
) -> list[_BundleEntry]:
    result: list[_BundleEntry] = []
    total = 0
    for origin in sorted(commits):
        origin_root = staging_root / origin
        actual_sections = {
            item.name
            for item in origin_root.iterdir()
            if item.is_dir() and not item.is_symlink()
        }
        if actual_sections != {"recipe", "distfiles"}:
            raise AlpineSourceError("origin staging sections are not closed")
        for section in ("distfiles", "recipe"):
            section_root = origin_root / section
            _regular_directory(section_root)
            for source in sorted(section_root.rglob("*")):
                status_result = source.lstat()
                if stat.S_ISDIR(status_result.st_mode):
                    continue
                if not stat.S_ISREG(status_result.st_mode):
                    raise AlpineSourceError("source input must be a regular file")
                relative = _safe_member_path(
                    source.relative_to(section_root).as_posix()
                )
                if status_result.st_size > MAX_BUNDLE_FILE_BYTES:
                    raise AlpineSourceError("source input file exceeds policy")
                total += status_result.st_size
                if total > MAX_BUNDLE_TOTAL_BYTES:
                    raise AlpineSourceError("source input total exceeds policy")
                body = source.read_bytes()
                if len(body) != status_result.st_size:
                    raise AlpineSourceError("source input file size changed")
                path = (PurePosixPath("origins", origin, section) / relative).as_posix()
                result.append(
                    _BundleEntry(
                        path=path,
                        role=section,
                        sha256=hashlib.sha256(body).hexdigest(),
                        size=len(body),
                        source=source,
                    )
                )
    if len(result) > MAX_BUNDLE_FILES:
        raise AlpineSourceError("source bundle file count exceeds policy")
    if not any(entry.path.endswith("/recipe/APKBUILD") for entry in result):
        raise AlpineSourceError("source bundle lacks APKBUILD recipes")
    return sorted(result, key=lambda entry: entry.path)


def _write_archive(
    entries: Sequence[_BundleEntry],
    manifest: bytes,
    output: Path,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and (output.is_symlink() or not output.is_file()):
        raise AlpineSourceError("source archive output must be a regular file")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        with (
            temporary_path.open("wb") as temporary,
            gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=temporary,
                mtime=0,
            ) as compressed,
            tarfile.open(fileobj=compressed, mode="w") as archive,
        ):
            _add_bytes(archive, "manifest.json", manifest)
            for entry in entries:
                _add_bytes(archive, entry.path, entry.source.read_bytes())
        if temporary_path.stat().st_size > MAX_BUNDLE_TOTAL_BYTES:
            raise AlpineSourceError("source archive exceeds policy")
        os.replace(temporary_path, output)
    except (OSError, tarfile.TarError) as error:
        raise AlpineSourceError("source archive creation failed") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def _apk_fields(record: str, record_number: int) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in record.splitlines():
        if len(line) < 2 or line[1] != ":":
            raise AlpineSourceError(f"invalid APK record {record_number}")
        key = line[0]
        if key in {"P", "V", "L", "o", "c"}:
            if key in fields:
                raise AlpineSourceError(
                    f"duplicate APK metadata in record {record_number}"
                )
            fields[key] = line[2:]
    return fields


def _package_from_fields(
    fields: Mapping[str, str], record_number: int
) -> AlpinePackage:
    try:
        package = AlpinePackage(
            name=fields["P"],
            version=fields["V"],
            license=fields["L"],
            origin=fields["o"],
            aports_commit=fields["c"],
        )
    except KeyError as error:
        raise AlpineSourceError(
            f"APK record {record_number} lacks required source metadata"
        ) from error
    _validate_package(package)
    return package


def _validate_package(package: AlpinePackage) -> None:
    if not SAFE_NAME_PATTERN.fullmatch(package.name):
        raise AlpineSourceError("unsafe Alpine package name")
    if not SAFE_VERSION_PATTERN.fullmatch(package.version):
        raise AlpineSourceError("unsafe Alpine package version")
    if (
        not package.license
        or len(package.license) > 512
        or any(ord(character) < 32 for character in package.license)
    ):
        raise AlpineSourceError("unsafe Alpine package license")
    _validate_origin(package.origin)
    _validate_commit(package.aports_commit)


def _validate_origin(origin: str) -> None:
    if not SAFE_NAME_PATTERN.fullmatch(origin):
        raise AlpineSourceError("unsafe origin")


def _validate_commit(commit: str) -> None:
    if not COMMIT_PATTERN.fullmatch(commit):
        raise AlpineSourceError("aports commit must be a lowercase 40-character SHA")


def _validate_aports_path(origin: str, value: str) -> None:
    if value not in {f"main/{origin}", f"community/{origin}"}:
        raise AlpineSourceError("reviewed aports path is invalid")


def _required_text(item: Mapping[str, object], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        raise AlpineSourceError(f"reviewed package {key} is invalid")
    return value


def _safe_member_path(value: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        raise AlpineSourceError("unsafe aports archive path")
    path = PurePosixPath(value.rstrip("/"))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise AlpineSourceError("unsafe aports archive path")
    if len(path.as_posix()) > 512:
        raise AlpineSourceError("aports archive path exceeds policy")
    return path


def _safe_link_target(member: PurePosixPath, value: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        raise AlpineSourceError("unsafe aports symlink target")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise AlpineSourceError("unsafe aports symlink target")
    return member.parent / relative


def _is_below(path: PurePosixPath, parent: PurePosixPath) -> bool:
    try:
        relative = path.relative_to(parent)
    except ValueError:
        return False
    return bool(relative.parts)


def _prepare_empty_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir() or any(path.iterdir()):
            raise AlpineSourceError("destination must be an empty regular directory")
    else:
        path.mkdir(parents=True)


def _write_new_file(path: Path, body: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(body)
    except OSError as error:
        raise AlpineSourceError("cannot create aports recipe file") from error


def _regular_directory(path: Path) -> None:
    try:
        status_result = path.lstat()
    except OSError as error:
        raise AlpineSourceError("required staging directory is missing") from error
    if not stat.S_ISDIR(status_result.st_mode) or path.is_symlink():
        raise AlpineSourceError("required staging path must be a regular directory")


def _validate_staging_root(staging_root: Path, expected: frozenset[str]) -> None:
    _regular_directory(staging_root)
    actual: set[str] = set()
    for entry in staging_root.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            raise AlpineSourceError("staging root contains an unexpected path")
        actual.add(entry.name)
    if actual != expected:
        raise AlpineSourceError("staging origins do not match package inventory")


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _add_bytes(archive: tarfile.TarFile, name: str, body: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(body)
    info.mode = 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    archive.addfile(info, io.BytesIO(body))
