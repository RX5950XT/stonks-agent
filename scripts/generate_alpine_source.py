#!/usr/bin/env python3
"""Create a deterministic, checksum-verified Alpine source bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.alpine_source_contract import (
    MAX_PACKAGE_DATABASE_BYTES,
    MAX_RECIPE_ARCHIVE_BYTES,
    AlpineSourceError,
    SourceArchiveSummary,
    build_aports_archive_url,
    create_source_archive,
    extract_recipe_archive,
    fetch_aports_archive,
    origin_commits,
    parse_apk_database,
    validate_reviewed_inventory,
    validated_origin_paths,
)
from scripts.alpine_source_contract import (
    AlpinePackage as AlpinePackage,
)
from scripts.alpine_source_contract import (
    SourceFile as SourceFile,
)

IMAGE_PATTERN = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|"
    r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64})$"
)
SANDBOX_PATTERN = re.compile(
    r"^[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+:[A-Za-z0-9._-]+"
    r"@sha256:[0-9a-f]{64}$"
)
CONTAINER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
DEFAULT_TIMEOUT_SECONDS = 900
DISTFILES_MIRROR = "https://distfiles.alpinelinux.org/distfiles/v3.23"
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
ArchiveFetcher = Callable[[str, int], bytes]


def run_fetch_verify_sandbox(
    *,
    origins: Sequence[str],
    staging_root: Path,
    sandbox_image: str,
    runner: CommandRunner = subprocess.run,
    container_name: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Fetch and checksum-verify source in an ephemeral mount-free sandbox."""
    if not SANDBOX_PATTERN.fullmatch(sandbox_image):
        raise AlpineSourceError("sandbox image must include tag and exact OCI digest")
    unique_origins = tuple(sorted(set(origins)))
    if not unique_origins or len(unique_origins) != len(origins):
        raise AlpineSourceError("sandbox origins must be unique")
    for origin in unique_origins:
        _validate_origin(origin)
        _regular_directory(staging_root / origin / "recipe")
        _prepare_empty_directory(staging_root / origin / "distfiles")
    name = container_name or f"stonks-alpine-source-{uuid.uuid4().hex[:16]}"
    if not CONTAINER_PATTERN.fullmatch(name):
        raise AlpineSourceError("unsafe sandbox container name")
    created = False
    try:
        _run(
            runner,
            (
                "docker",
                "create",
                "--name",
                name,
                "--network",
                "bridge",
                "--security-opt",
                "no-new-privileges",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=64m",
                sandbox_image,
                "tail",
                "-f",
                "/dev/null",
            ),
            timeout_seconds,
            "sandbox creation failed",
        )
        created = True
        _run(runner, ("docker", "start", name), timeout_seconds, "sandbox start failed")
        _run(
            runner,
            (
                "docker",
                "exec",
                name,
                "apk",
                "add",
                "--no-cache",
                "alpine-sdk=1.1-r0",
            ),
            timeout_seconds,
            "sandbox tool installation failed",
        )
        _run(
            runner,
            ("docker", "exec", name, "adduser", "-D", "-h", "/home/builder", "builder"),
            timeout_seconds,
            "sandbox user creation failed",
        )
        for origin in unique_origins:
            _sandbox_origin(
                runner=runner,
                name=name,
                origin=origin,
                staging_root=staging_root,
                timeout_seconds=timeout_seconds,
            )
    finally:
        if created:
            _remove_container(runner, name)


def generate_corresponding_source(
    *,
    image: str,
    legal_path: Path,
    output: Path,
    sandbox_image: str,
    runner: CommandRunner = subprocess.run,
    fetcher: ArchiveFetcher = fetch_aports_archive,
) -> SourceArchiveSummary:
    """Reconcile the image inventory and create its verified source archive."""
    if not IMAGE_PATTERN.fullmatch(image):
        raise AlpineSourceError(
            "core image must be an exact image ID or registry digest"
        )
    legal = _load_json(legal_path, max_bytes=4 * 1024 * 1024)
    alpine = legal.get("alpine")
    if not isinstance(alpine, Mapping):
        raise AlpineSourceError("core runtime legal policy lacks Alpine metadata")
    source_policy = alpine.get("corresponding_source")
    if not isinstance(source_policy, Mapping):
        raise AlpineSourceError("Alpine corresponding-source policy is missing")
    with tempfile.TemporaryDirectory(prefix="stonks-alpine-source-") as directory:
        temporary_root = Path(directory)
        database = _read_image_package_database(
            image=image,
            destination=temporary_root / "installed",
            runner=runner,
        )
        packages = parse_apk_database(database.decode("utf-8"))
        validate_reviewed_inventory(packages, alpine.get("packages"))
        commits = origin_commits(packages)
        paths = validated_origin_paths(commits, source_policy.get("origin_paths"))
        staging = temporary_root / "origins"
        staging.mkdir()
        _download_recipes(
            commits=commits,
            paths=paths,
            staging=staging,
            fetcher=fetcher,
        )
        run_fetch_verify_sandbox(
            origins=tuple(commits),
            staging_root=staging,
            sandbox_image=sandbox_image,
            runner=runner,
        )
        return create_source_archive(
            packages=packages,
            staging_root=staging,
            output=output,
            package_database_sha256=hashlib.sha256(database).hexdigest(),
            origin_paths=paths,
        )


def _download_recipes(
    *,
    commits: Mapping[str, str],
    paths: Mapping[str, str],
    staging: Path,
    fetcher: ArchiveFetcher,
) -> None:
    for origin, commit in commits.items():
        recipe = staging / origin / "recipe"
        recipe.parent.mkdir()
        try:
            archive = fetcher(
                build_aports_archive_url(
                    origin,
                    commit,
                    aports_path=paths[origin],
                ),
                MAX_RECIPE_ARCHIVE_BYTES,
            )
            extract_recipe_archive(
                archive,
                origin=origin,
                commit=commit,
                destination=recipe,
                aports_path=paths[origin],
            )
        except AlpineSourceError as error:
            raise AlpineSourceError(
                f"aports recipe closure failed for {origin}: {error}"
            ) from error


def _sandbox_origin(
    *,
    runner: CommandRunner,
    name: str,
    origin: str,
    staging_root: Path,
    timeout_seconds: int,
) -> None:
    container_root = f"/work/origins/{origin}"
    _run(
        runner,
        (
            "docker",
            "exec",
            name,
            "mkdir",
            "-p",
            f"{container_root}/recipe",
            f"{container_root}/distfiles",
        ),
        timeout_seconds,
        "sandbox source directory creation failed",
    )
    _run(
        runner,
        (
            "docker",
            "cp",
            f"{staging_root / origin / 'recipe'}/.",
            f"{name}:{container_root}/recipe",
        ),
        timeout_seconds,
        "sandbox recipe copy failed",
    )
    _run(
        runner,
        ("docker", "exec", name, "chown", "-R", "builder:builder", container_root),
        timeout_seconds,
        "sandbox ownership setup failed",
    )
    _run(
        runner,
        (
            "docker",
            "exec",
            "--env",
            f"SRCDEST={container_root}/distfiles",
            "--env",
            f"DISTFILES_MIRROR={DISTFILES_MIRROR}",
            "--workdir",
            f"{container_root}/recipe",
            "--user",
            "builder",
            name,
            "abuild",
            "fetch",
            "verify",
        ),
        timeout_seconds,
        f"abuild fetch verify failed for {origin}",
    )
    _run(
        runner,
        (
            "docker",
            "cp",
            f"{name}:{container_root}/distfiles/.",
            str(staging_root / origin / "distfiles"),
        ),
        timeout_seconds,
        "verified source copy failed",
    )


def _read_image_package_database(
    *,
    image: str,
    destination: Path,
    runner: CommandRunner,
) -> bytes:
    name = f"stonks-apk-db-{uuid.uuid4().hex[:16]}"
    created = False
    try:
        _run(
            runner,
            ("docker", "create", "--name", name, "--network", "none", image),
            120,
            "core image inspection container creation failed",
        )
        created = True
        _run(
            runner,
            (
                "docker",
                "cp",
                f"{name}:/lib/apk/db/installed",
                str(destination),
            ),
            120,
            "core image APK database copy failed",
        )
        status_result = destination.lstat()
        if not stat.S_ISREG(status_result.st_mode):
            raise AlpineSourceError("core image APK database must be a regular file")
        if status_result.st_size > MAX_PACKAGE_DATABASE_BYTES:
            raise AlpineSourceError("APK package database exceeds policy")
        return destination.read_bytes()
    finally:
        if created:
            _remove_container(runner, name)


def _run(
    runner: CommandRunner,
    command: tuple[str, ...],
    timeout_seconds: int,
    message: str,
) -> None:
    try:
        runner(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AlpineSourceError(message) from error


def _remove_container(runner: CommandRunner, name: str) -> None:
    try:
        runner(
            ("docker", "rm", "--force", name),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AlpineSourceError("sandbox cleanup failed") from error


def _prepare_empty_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir() or any(path.iterdir()):
            raise AlpineSourceError("destination must be an empty regular directory")
    else:
        path.mkdir(parents=True)


def _regular_directory(path: Path) -> None:
    try:
        status_result = path.lstat()
    except OSError as error:
        raise AlpineSourceError("required staging directory is missing") from error
    if not stat.S_ISDIR(status_result.st_mode) or path.is_symlink():
        raise AlpineSourceError("required staging path must be a regular directory")


def _validate_origin(origin: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+._-]{0,127}", origin):
        raise AlpineSourceError("unsafe origin")


def _load_json(path: Path, *, max_bytes: int) -> dict[str, Any]:
    try:
        status_result = path.lstat()
        if not stat.S_ISREG(status_result.st_mode) or status_result.st_size > max_bytes:
            raise AlpineSourceError("legal policy must be a bounded regular file")
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AlpineSourceError("invalid legal policy JSON") from error
    if not isinstance(payload, dict):
        raise AlpineSourceError("legal policy JSON root must be an object")
    return payload


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument(
        "--legal",
        type=Path,
        default=Path("config/release/core-runtime-legal.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sandbox-image", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = generate_corresponding_source(
            image=args.image,
            legal_path=args.legal,
            output=args.output,
            sandbox_image=args.sandbox_image,
        )
    except AlpineSourceError as error:
        print(f"Alpine source generation failed: {error}")
        return 1
    print(json.dumps(asdict(summary), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
