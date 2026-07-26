"""Shared fail-closed primitives for release verification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST_SCHEMA = "stonks-agent/release-manifest/v1"
REPORT_SCHEMA = "stonks-agent/release-verification/v1"
IMAGE_PATTERN = re.compile(r"^ghcr\.io/[a-z0-9_.-]+/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SAFE_PATH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


class ReleaseError(ValueError):
    """Raised when a release artifact violates the frozen policy."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.phase = "unknown"


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def load_json(path: Path, *, max_bytes: int) -> dict[str, Any]:
    status_result = regular_status(path, max_bytes=max_bytes)
    if status_result.st_size < 2:
        raise ReleaseError("JSON file is empty")
    try:
        payload = json.loads(
            path.read_bytes(),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except ReleaseError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseError("invalid JSON document") from error
    if not isinstance(payload, dict):
        raise ReleaseError("JSON root must be an object")
    return payload


def safe_join(root: Path, relative: str) -> Path:
    validate_relative_path(relative, label="release policy path")
    candidate = root / PurePosixPath(relative)
    try:
        resolved_root = root.resolve(strict=True)
        resolved_parent = candidate.parent.resolve(strict=True)
    except OSError as error:
        raise ReleaseError("release policy path is missing") from error
    if not resolved_parent.is_relative_to(resolved_root):
        raise ReleaseError("release policy path escapes bundle")
    return candidate


def repository_path(root: Path, relative: str) -> Path:
    validate_relative_path(relative, label="repository path")
    try:
        candidate = (root / PurePosixPath(relative)).resolve(strict=True)
    except OSError as error:
        raise ReleaseError("repository path is missing") from error
    if not candidate.is_relative_to(root):
        raise ReleaseError("repository path escapes root")
    return candidate


def validate_relative_path(value: str, *, label: str) -> None:
    if (
        not value
        or not SAFE_PATH_PATTERN.fullmatch(value)
        or "\\" in value
        or value.startswith("/")
    ):
        raise ReleaseError(f"unsafe {label}: {value}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseError(f"unsafe {label}: {value}")


def regular_status(path: Path, *, max_bytes: int) -> os.stat_result:
    try:
        result = path.lstat()
    except OSError as error:
        raise ReleaseError("required regular file is missing") from error
    if not stat.S_ISREG(result.st_mode) or path.is_symlink():
        raise ReleaseError("release entry must be a regular file")
    if result.st_size < 0 or result.st_size > max_bytes:
        raise ReleaseError("release file size is outside policy")
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise ReleaseError("release file cannot be hashed") from error
    return digest.hexdigest()


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()


def as_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ReleaseError(f"{label} contains a non-string key")
    return value


def as_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseError(f"{label} must be a non-empty string")
    return value


def positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ReleaseError(f"{label} must be a positive integer")
    return value


def unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ReleaseError(f"non-finite JSON number is forbidden: {value}")
