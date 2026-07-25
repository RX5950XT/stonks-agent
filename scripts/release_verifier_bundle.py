"""Release bundle inventory, staging, identity, and lock contracts."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from scripts.release_verifier_common import (
    COMMIT_PATTERN,
    IMAGE_PATTERN,
    MANIFEST_SCHEMA,
    REPOSITORY_PATTERN,
    SHA256_PATTERN,
    VERSION_PATTERN,
    CommandRunner,
    ReleaseError,
    as_mapping,
    as_string,
    load_json,
    positive_int,
    regular_status,
    repository_path,
    sha256,
    validate_relative_path,
)


def create_manifest(
    bundle: Path,
    policy: Mapping[str, Any],
    *,
    version: str,
    tag: str,
    repository: str,
    commit: str,
    image: str,
    signing_mode: str,
) -> dict[str, Any]:
    validate_identity(
        version=version,
        tag=tag,
        repository=repository,
        commit=commit,
        image=image,
        signing_mode=signing_mode,
    )
    limits = bundle_limits(policy)
    files = inventory_files(bundle / "payload", limits)
    required = required_payload_files(policy)
    actual = {item["path"] for item in files}
    missing = sorted(required - actual)
    if missing:
        raise ReleaseError(f"required payload file is missing: {missing[0]}")
    verify_required_trees(policy, actual)
    return {
        "schema_version": MANIFEST_SCHEMA,
        "release": {
            "name": "stonks-agent",
            "version": version,
            "tag": tag,
            "repository": repository,
            "commit": commit,
            "execution_mode": "paper",
        },
        "image": {
            "subject": image,
            "digest": image.rsplit("@", maxsplit=1)[1],
        },
        "signing_mode": signing_mode,
        "artifacts": files,
    }


def audit_locks(
    root: Path,
    policy: Mapping[str, Any],
    *,
    runner: CommandRunner = subprocess.run,
) -> dict[str, Any]:
    resolved = root.resolve(strict=True)
    lock_policy = as_mapping(policy.get("locks"), "policy.locks")
    projects = lock_policy.get("uv_projects")
    if not isinstance(projects, list) or not projects:
        raise ReleaseError("uv lock project policy is empty")
    results: list[dict[str, str]] = []
    for raw in projects:
        project = as_string(raw, "uv lock project")
        project_path = (
            resolved if project == "." else repository_path(resolved, project)
        )
        lock_path = project_path / "uv.lock"
        verify_uv_lock(lock_path)
        completed = runner(
            ("uv", "lock", "--check", "--project", str(project_path)),
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if completed.returncode != 0:
            raise ReleaseError(f"uv lock check failed: {project}")
        results.append(
            {
                "project": project,
                "lock": "uv.lock" if project == "." else f"{project}/uv.lock",
                "sha256": sha256(lock_path),
                "status": "passed",
            }
        )
    nuget_root = repository_path(
        resolved,
        as_string(lock_policy.get("nuget_tree"), "locks.nuget_tree"),
    )
    nuget_files = sorted(nuget_root.rglob("packages.lock.json"))
    expected_count = positive_int(
        lock_policy.get("nuget_lock_count"), "locks.nuget_lock_count"
    )
    if len(nuget_files) != expected_count:
        raise ReleaseError("NuGet lock tree count drifted")
    for path in nuget_files:
        document = load_json(path, max_bytes=16 * 1024 * 1024)
        if document.get("version") != 1 or not isinstance(
            document.get("dependencies"), Mapping
        ):
            raise ReleaseError("NuGet lock document is invalid")
    return {
        "success": True,
        "status": "passed",
        "data": {
            "projects": results,
            "nuget_lock_count": len(nuget_files),
        },
        "error": None,
    }


def stage_release(root: Path, bundle: Path, policy: Mapping[str, Any]) -> int:
    resolved_root = root.resolve(strict=True)
    resolved_bundle = bundle.resolve()
    if resolved_bundle == resolved_root or resolved_bundle.is_relative_to(
        resolved_root
    ):
        raise ReleaseError("release bundle must be outside the source tree")
    if bundle.exists():
        if bundle.is_symlink() or not bundle.is_dir() or any(bundle.iterdir()):
            raise ReleaseError("release staging directory must be empty")
    else:
        bundle.mkdir(parents=True)
    copied: set[str] = set()
    for target in sorted(required_payload_files(policy)):
        if target.startswith("payload/release/"):
            continue
        source_relative = target.removeprefix("payload/")
        source = repository_path(resolved_root, source_relative)
        copy_release_file(source, bundle / PurePosixPath(target))
        copied.add(target)
    bundle_policy = as_mapping(policy.get("bundle"), "policy.bundle")
    trees = bundle_policy.get("required_trees", [])
    if not isinstance(trees, list):
        raise ReleaseError("required payload trees must be a list")
    for raw in trees:
        item = as_mapping(raw, "required payload tree")
        target_root = as_string(item.get("path"), "required tree path")
        suffix = as_string(item.get("suffix"), "required tree suffix")
        source_root = repository_path(
            resolved_root, target_root.removeprefix("payload/")
        )
        for source in sorted(source_root.rglob(f"*{suffix}")):
            relative = source.relative_to(source_root).as_posix()
            target = f"{target_root}/{relative}"
            if target in copied:
                continue
            copy_release_file(source, bundle / PurePosixPath(target))
            copied.add(target)
    return len(copied)


def verify_manifest_shape(manifest: Mapping[str, Any]) -> None:
    if set(manifest) != {
        "schema_version",
        "release",
        "image",
        "signing_mode",
        "artifacts",
    }:
        raise ReleaseError("release manifest contains unknown or missing fields")
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise ReleaseError("release manifest schema is invalid")
    release = as_mapping(manifest.get("release"), "manifest.release")
    if set(release) != {
        "name",
        "version",
        "tag",
        "repository",
        "commit",
        "execution_mode",
    }:
        raise ReleaseError("release identity fields drifted")
    if release.get("name") != "stonks-agent":
        raise ReleaseError("release name is invalid")
    image = as_mapping(manifest.get("image"), "manifest.image")
    if set(image) != {"subject", "digest"}:
        raise ReleaseError("image identity fields drifted")
    subject = image.get("subject")
    if isinstance(subject, str) and image.get("digest") != subject.rsplit("@", 1)[-1]:
        raise ReleaseError("image digest field drifted")


def artifact_entries(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ReleaseError("manifest artifacts must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    previous = ""
    for raw in value:
        item = as_mapping(raw, "manifest artifact")
        if set(item) != {"path", "sha256", "size"}:
            raise ReleaseError("manifest artifact fields drifted")
        path = as_string(item.get("path"), "artifact.path")
        validate_relative_path(path, label="artifact path")
        digest = as_string(item.get("sha256"), "artifact.sha256")
        size = item.get("size")
        if (
            not SHA256_PATTERN.fullmatch(digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise ReleaseError("manifest artifact hash or size is invalid")
        if path in seen or (previous and path <= previous):
            raise ReleaseError("manifest artifacts must be sorted and unique")
        seen.add(path)
        previous = path
        result.append({"path": path, "sha256": digest, "size": size})
    if not result:
        raise ReleaseError("manifest artifact inventory is empty")
    return result


def inventory_files(
    payload: Path, limits: tuple[int, int, int]
) -> list[dict[str, Any]]:
    max_files, max_file_bytes, max_total_bytes = limits
    try:
        status_result = payload.lstat()
    except OSError as error:
        raise ReleaseError("payload directory is missing") from error
    if not stat.S_ISDIR(status_result.st_mode) or payload.is_symlink():
        raise ReleaseError("payload must be a regular directory")
    files: list[tuple[str, Path]] = []
    for current, directories, names in os.walk(payload, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            if (current_path / directory).is_symlink():
                raise ReleaseError("payload entries must be regular files")
        for name in names:
            candidate = current_path / name
            relative = "payload/" + candidate.relative_to(payload).as_posix()
            validate_relative_path(relative, label="payload path")
            files.append((relative, candidate))
    files.sort(key=lambda item: item[0])
    if not files or len(files) > max_files:
        raise ReleaseError("payload file count is outside policy")
    return _inventory_entries(files, max_file_bytes, max_total_bytes)


def _inventory_entries(
    files: list[tuple[str, Path]], max_file_bytes: int, max_total_bytes: int
) -> list[dict[str, Any]]:
    lowered: set[str] = set()
    total = 0
    entries: list[dict[str, Any]] = []
    for relative, path in files:
        folded = relative.casefold()
        if folded in lowered:
            raise ReleaseError(f"case-colliding payload path: {relative}")
        lowered.add(folded)
        status_result = regular_status(path, max_bytes=max_file_bytes)
        if status_result.st_nlink > 1:
            raise ReleaseError("payload entries must not be hardlinks")
        total += status_result.st_size
        if total > max_total_bytes:
            raise ReleaseError("payload total size exceeds policy")
        entries.append(
            {
                "path": relative,
                "sha256": sha256(path),
                "size": status_result.st_size,
            }
        )
    return entries


def validate_identity(
    *,
    version: str,
    tag: str,
    repository: str,
    commit: str,
    image: str,
    signing_mode: str,
) -> None:
    if not VERSION_PATTERN.fullmatch(version) or tag != f"v{version}":
        raise ReleaseError("release version and exact SemVer tag are invalid")
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise ReleaseError("repository slug is invalid")
    if not COMMIT_PATTERN.fullmatch(commit):
        raise ReleaseError("release commit must be a full lowercase SHA")
    if not IMAGE_PATTERN.fullmatch(image):
        raise ReleaseError("image must be an exact registry image digest")
    image_repository, separator, _digest = image.partition("@sha256:")
    expected_repository = f"ghcr.io/{repository.lower()}"
    if separator != "@sha256:" or image_repository != expected_repository:
        raise ReleaseError("image repository does not match release repository")
    if signing_mode not in {"unsigned-candidate", "keyless-release"}:
        raise ReleaseError("release signing mode is invalid")


def bundle_limits(policy: Mapping[str, Any]) -> tuple[int, int, int]:
    bundle = as_mapping(policy.get("bundle"), "policy.bundle")
    return (
        positive_int(bundle.get("max_files"), "bundle.max_files"),
        positive_int(bundle.get("max_file_bytes"), "bundle.max_file_bytes"),
        positive_int(bundle.get("max_total_bytes"), "bundle.max_total_bytes"),
    )


def required_payload_files(policy: Mapping[str, Any]) -> set[str]:
    bundle = as_mapping(policy.get("bundle"), "policy.bundle")
    raw = bundle.get("required_payload_files")
    if not isinstance(raw, list) or not raw:
        raise ReleaseError("required payload files must be a non-empty list")
    result: set[str] = set()
    for value in raw:
        path = as_string(value, "required payload path")
        validate_relative_path(path, label="required payload path")
        if not path.startswith("payload/") or path in result:
            raise ReleaseError("required payload paths are invalid")
        result.add(path)
    return result


def verify_required_trees(policy: Mapping[str, Any], actual_paths: set[str]) -> None:
    bundle = as_mapping(policy.get("bundle"), "policy.bundle")
    trees = bundle.get("required_trees", [])
    if not isinstance(trees, list):
        raise ReleaseError("required payload trees must be a list")
    for raw in trees:
        item = as_mapping(raw, "required payload tree")
        if set(item) != {"path", "suffix", "file_count"}:
            raise ReleaseError("required payload tree fields drifted")
        root = as_string(item.get("path"), "required tree path").rstrip("/")
        suffix = as_string(item.get("suffix"), "required tree suffix")
        count = positive_int(item.get("file_count"), "required tree file count")
        validate_relative_path(root, label="required tree path")
        matching = {
            path
            for path in actual_paths
            if path.startswith(f"{root}/") and path.endswith(suffix)
        }
        if len(matching) != count:
            raise ReleaseError(f"required payload tree count drifted: {root}")


def copy_release_file(source: Path, target: Path) -> None:
    status_result = regular_status(source, max_bytes=536_870_912)
    if status_result.st_nlink > 1:
        raise ReleaseError("source release file must not be a hardlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise ReleaseError("release staging target already exists")
    try:
        shutil.copyfile(source, target, follow_symlinks=False)
    except OSError as error:
        raise ReleaseError("release staging copy failed") from error


def verify_uv_lock(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ReleaseError("uv lock document is invalid") from error
    if (
        document.get("version") != 1
        or not isinstance(document.get("revision"), int)
        or not isinstance(document.get("package"), list)
        or not document["package"]
    ):
        raise ReleaseError("uv lock document is incomplete")
