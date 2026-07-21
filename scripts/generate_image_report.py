#!/usr/bin/env python3
"""Generate a closed identity report for one exact release container image."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

IMAGE_PATTERN = re.compile(r"^ghcr\.io/[a-z0-9_.-]+/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
LOCAL_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
REPORT_SCHEMA = "stonks-agent/core-image/v1"


class ImageReportError(ValueError):
    """Raised when the inspected image identity violates release policy."""


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def build_image_report(
    payload: object,
    *,
    subject: str,
    repository: str,
    commit: str,
    version: str,
    require_registry_digest: bool = True,
) -> dict[str, object]:
    _validate_inputs(
        subject=subject,
        repository=repository,
        commit=commit,
        version=version,
    )
    if not isinstance(payload, list) or len(payload) != 1:
        raise ImageReportError("Docker inspect must return exactly one image")
    image = _mapping(payload[0], "Docker image")
    config_digest = image.get("Id")
    if not isinstance(config_digest, str) or not DIGEST_PATTERN.fullmatch(
        config_digest
    ):
        raise ImageReportError("image config digest is invalid")
    repo_digests = image.get("RepoDigests")
    if not isinstance(repo_digests, list) or (
        require_registry_digest and subject not in repo_digests
    ):
        raise ImageReportError("exact repository digest is absent")
    config = _mapping(image.get("Config"), "Docker image config")
    if config.get("User") != "65532:65532":
        raise ImageReportError("release image is not the expected non-root user")
    labels = _mapping(config.get("Labels"), "OCI labels")
    source = f"https://github.com/{repository}"
    expected = {
        "org.opencontainers.image.licenses": "Apache-2.0",
        "org.opencontainers.image.revision": commit,
        "org.opencontainers.image.source": source,
        "org.opencontainers.image.version": version,
    }
    for name, value in expected.items():
        if labels.get(name) != value:
            label = name.rsplit(".", maxsplit=1)[-1]
            raise ImageReportError(f"OCI {label} identity drifted")
    return {
        "schema_version": REPORT_SCHEMA,
        "subject": subject,
        "digest": subject.rsplit("@", maxsplit=1)[1],
        "config_digest": config_digest,
        "repository": repository,
        "revision": commit,
        "version": version,
        "source": source,
        "licenses": "Apache-2.0",
        "user": "65532:65532",
        "execution_mode": "paper",
        "registry_verified": require_registry_digest,
    }


def generate_image_report(
    *,
    local_reference: str,
    subject: str,
    repository: str,
    commit: str,
    version: str,
    output: Path,
    runner: CommandRunner = subprocess.run,
    require_registry_digest: bool = True,
) -> dict[str, object]:
    _validate_inputs(
        subject=subject,
        repository=repository,
        commit=commit,
        version=version,
    )
    if not LOCAL_REFERENCE_PATTERN.fullmatch(local_reference):
        raise ImageReportError("local image reference is unsafe")
    completed = runner(
        ("docker", "image", "inspect", local_reference),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ImageReportError("Docker image inspect failed")
    try:
        payload = json.loads(
            completed.stdout,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ImageReportError, json.JSONDecodeError) as error:
        raise ImageReportError("Docker image inspect returned invalid JSON") from error
    report = build_image_report(
        payload,
        subject=subject,
        repository=repository,
        commit=commit,
        version=version,
        require_registry_digest=require_registry_digest,
    )
    _atomic_write(output, _json_bytes(report))
    return report


def _validate_inputs(
    *,
    subject: str,
    repository: str,
    commit: str,
    version: str,
) -> None:
    if not IMAGE_PATTERN.fullmatch(subject):
        raise ImageReportError("subject must be an exact registry image digest")
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise ImageReportError("repository identity is invalid")
    if not COMMIT_PATTERN.fullmatch(commit):
        raise ImageReportError("revision must be a full lowercase commit")
    if not VERSION_PATTERN.fullmatch(version):
        raise ImageReportError("release version is invalid")


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ImageReportError(f"{label} is invalid")
    return value


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ImageReportError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ImageReportError(f"non-finite JSON number: {value}")


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _atomic_write(path: Path, payload: bytes) -> None:
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir() or path.parent.is_symlink():
        raise ImageReportError("output directory must be regular")
    if path.exists():
        status_result = path.lstat()
        if not stat.S_ISREG(status_result.st_mode) or path.is_symlink():
            raise ImageReportError("output path must be a regular file")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise ImageReportError("image report cannot be written") from error
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-reference", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allow-local-candidate", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        report = generate_image_report(
            local_reference=args.local_reference,
            subject=args.subject,
            repository=args.repository,
            commit=args.commit,
            version=args.version,
            output=args.output,
            require_registry_digest=not args.allow_local_candidate,
        )
        result: dict[str, object] = {
            "success": True,
            "status": "passed",
            "data": report,
            "error": None,
        }
    except (ImageReportError, OSError, subprocess.SubprocessError):
        result = {
            "success": False,
            "status": "failed",
            "data": None,
            "error": {"code": "IMAGE_IDENTITY_INVALID"},
        }
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["success"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
