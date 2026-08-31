"""Bounded, secret-safe subprocess and file helpers for restore drills."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from scripts.postgres_restore_contract import (
    MAX_COMMAND_OUTPUT_BYTES,
    RestoreDrillError,
)


def run_text(
    command: Sequence[str],
    *,
    timeout: float,
    secret: str,
) -> str:
    _assert_secret_free(command, secret)
    try:
        completed = subprocess.run(
            tuple(command),
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RestoreDrillError from error
    size = len(completed.stdout.encode()) + len(completed.stderr.encode())
    if completed.returncode != 0 or size > MAX_COMMAND_OUTPUT_BYTES:
        raise RestoreDrillError
    if secret in completed.stdout or secret in completed.stderr:
        raise RestoreDrillError
    return completed.stdout


def optional_text(command: Sequence[str], *, timeout: float) -> str | None:
    try:
        completed = subprocess.run(
            tuple(command),
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RestoreDrillError from error
    size = len(completed.stdout.encode()) + len(completed.stderr.encode())
    if size > MAX_COMMAND_OUTPUT_BYTES:
        raise RestoreDrillError
    return completed.stdout if completed.returncode == 0 else None


def run_status(command: Sequence[str], *, timeout: float) -> int:
    try:
        completed = subprocess.run(
            tuple(command),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RestoreDrillError from error
    return completed.returncode


def run_bounded_output(
    command: Sequence[str],
    *,
    output: Path,
    max_bytes: int,
    timeout: float,
    secret: str,
) -> None:
    _assert_secret_free(command, secret)
    stderr_path = output.with_suffix(".stderr")
    try:
        with output.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                tuple(command),
                stdout=stdout,
                stderr=stderr,
                shell=False,
            )
            _wait_bounded_process(
                process,
                paths=((output, max_bytes), (stderr_path, MAX_COMMAND_OUTPUT_BYTES)),
                timeout=timeout,
            )
        if secret.encode() in stderr_path.read_bytes():
            raise RestoreDrillError
    except (OSError, subprocess.SubprocessError) as error:
        raise RestoreDrillError from error
    finally:
        stderr_path.unlink(missing_ok=True)


def run_bounded_input(
    command: Sequence[str],
    *,
    input_path: Path,
    timeout: float,
    secret: str,
) -> None:
    _assert_secret_free(command, secret)
    with tempfile.TemporaryDirectory(prefix="stonks-pg-input-") as raw:
        directory = Path(raw)
        stdout_path = directory / "stdout"
        stderr_path = directory / "stderr"
        try:
            with (
                input_path.open("rb") as stdin,
                stdout_path.open("wb") as stdout,
                stderr_path.open("wb") as stderr,
            ):
                process = subprocess.Popen(
                    tuple(command),
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    shell=False,
                )
                _wait_bounded_process(
                    process,
                    paths=(
                        (stdout_path, MAX_COMMAND_OUTPUT_BYTES),
                        (stderr_path, MAX_COMMAND_OUTPUT_BYTES),
                    ),
                    timeout=timeout,
                )
            if any(
                secret.encode() in path.read_bytes()
                for path in (stdout_path, stderr_path)
            ):
                raise RestoreDrillError
        except (OSError, subprocess.SubprocessError) as error:
            raise RestoreDrillError from error


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path: Path, payload: bytes) -> None:
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir() or path.parent.is_symlink():
        raise RestoreDrillError
    if path.exists():
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode) or path.is_symlink():
            raise RestoreDrillError
    descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise RestoreDrillError from error
    finally:
        temporary.unlink(missing_ok=True)


def _wait_bounded_process(
    process: subprocess.Popen[bytes],
    *,
    paths: Sequence[tuple[Path, int]],
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        oversized = any(path.stat().st_size > maximum for path, maximum in paths)
        if time.monotonic() >= deadline or oversized:
            process.kill()
            process.wait(timeout=10)
            raise RestoreDrillError
        time.sleep(0.05)
    if process.returncode != 0 or any(
        path.stat().st_size > maximum for path, maximum in paths
    ):
        raise RestoreDrillError


def _assert_secret_free(command: Sequence[str], secret: str) -> None:
    if not secret or any(secret in item for item in command):
        raise RestoreDrillError
