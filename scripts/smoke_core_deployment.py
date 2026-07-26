#!/usr/bin/env python3
"""Exercise the hardened core image and durable PostgreSQL replay boundary."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

ROOT = Path(__file__).resolve().parents[1]
_HTTP_TIMEOUT_SECONDS = 60.0
_HTTP_REQUEST_TIMEOUT_SECONDS = 5.0
_COMMAND_TIMEOUT_SECONDS = 900.0
_MAX_REPLAY_PROBE_BYTES = 128 * 1024
_COMPOSE_ACTIONS = frozenset(
    {
        "build",
        "config",
        "down",
        "exec",
        "logs",
        "ps",
        "restart",
        "run",
        "start",
        "stop",
        "up",
    }
)
_COMPOSE_SERVICES = frozenset({"core", "migrate", "postgres"})
_IMAGE_CONTENT_CHECK = """
import hashlib
import importlib.util
import os
import pathlib
import shutil

assert os.getuid() == 65532
assert all(
    shutil.which(tool) is None
    for tool in ("uv", "pip", "pip3", "cc", "gcc", "g++", "make", "cmake")
)
assert importlib.util.find_spec("pip") is None
assert all(
    importlib.util.find_spec(module) is None
    for module in ("openbb", "torch", "tradingagents", "qlib")
)
assert not pathlib.Path("/opt/stonks/tests").exists()
assert not pathlib.Path("/opt/stonks/.research").exists()
notice_path = pathlib.Path(
    "/usr/share/licenses/stonks-agent/"
    "AI-HEDGE-FUND-MIT-PEAD-EVENT-STUDY.md"
)
notice = notice_path.read_bytes()
assert hashlib.sha256(notice).hexdigest() == (
    "91607e5dd43d93ad8372921ceacba8a579b07dcd6cd2dd5a2be244d8e6e7696c"
)
notice_text = notice.decode("utf-8")
assert all(
    marker in notice_text
    for marker in (
        "Copyright (c) 2024 Virat Singh",
        "Permission is hereby granted, free of charge",
        'THE SOFTWARE IS PROVIDED "AS IS"',
    )
)
"""
_READ_ONLY_ROOTFS_CHECK = """
from pathlib import Path

blocked = Path("/opt/stonks/write-probe")
try:
    blocked.write_text("blocked", encoding="utf-8")
except OSError:
    pass
else:
    raise SystemExit(1)
allowed = Path("/tmp/write-probe")
allowed.write_text("allowed", encoding="utf-8")
allowed.unlink()
"""
_SAFE_AMBIENT_ENVIRONMENT = frozenset(
    {
        "APPDATA",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
        "XDG_CONFIG_HOME",
    }
)


class SmokeError(RuntimeError):
    """Public-safe deployment smoke failure."""

    def __init__(self, phase: str = "unknown") -> None:
        super().__init__("Core deployment smoke failed")
        self.phase = (
            phase if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", phase) else "unknown"
        )


class CommandRunner(Protocol):
    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> str: ...


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> object: ...


@dataclass(frozen=True)
class SmokeContext:
    root: Path
    compose_file: Path
    replay_probe: Path
    owner_secret_file: Path
    runtime_secret_file: Path
    secret_values: tuple[str, str]
    environment: Mapping[str, str]
    project_name: str
    build_revision: str
    core_port: int

    @property
    def compose_prefix(self) -> tuple[str, ...]:
        return (
            "docker",
            "compose",
            "--project-name",
            self.project_name,
            "--file",
            str(self.compose_file),
        )

    @property
    def image_ref(self) -> str:
        return f"stonks-agent-core:{self.build_revision}"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.core_port}"


class SubprocessRunner:
    """Run argv-only child processes while withholding all captured diagnostics."""

    def __init__(
        self,
        *,
        cwd: Path,
        environment: Mapping[str, str],
        secret_values: Sequence[str],
        timeout_seconds: float = _COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        self._cwd = cwd
        self._environment = dict(environment)
        self._secret_values = tuple(secret_values)
        self._timeout_seconds = timeout_seconds

    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> str:
        try:
            completed = subprocess.run(
                tuple(command),
                cwd=self._cwd,
                env=self._environment,
                input=input_text,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds,
                check=False,
            )
            rendered = f"{completed.stdout}\n{completed.stderr}"
            _reject_secret_output(rendered, self._secret_values)
            if completed.returncode != 0:
                raise SmokeError(_command_phase(command))
            return completed.stdout.strip()
        except SmokeError:
            raise
        except (OSError, subprocess.SubprocessError) as error:
            raise SmokeError(_command_phase(command)) from error


type EndpointWaiter = Callable[[SmokeContext, str, int], None]


def build_context(
    *,
    root: Path,
    secret_directory: Path,
    base_environment: Mapping[str, str],
    core_port: int,
    project_name: str | None = None,
    build_revision: str | None = None,
    secret_values: tuple[str, str] | None = None,
) -> SmokeContext:
    """Create an isolated Compose identity and file-only database credentials."""

    if not 1 <= core_port <= 65_535:
        raise SmokeError()
    token = (
        secrets.token_hex(6) if project_name is None or build_revision is None else ""
    )
    project = project_name or f"stonks-smoke-{token}"
    revision = build_revision or token
    values = secret_values or (
        secrets.token_urlsafe(32),
        secrets.token_urlsafe(32),
    )
    _validate_generated_values(project, revision, values)
    secret_directory.mkdir(parents=True, exist_ok=False)
    secret_directory.chmod(0o700)
    owner_file = (secret_directory / "postgres-owner").resolve()
    runtime_file = (secret_directory / "stonks-runtime").resolve()
    _write_secret(owner_file, values[0])
    _write_secret(runtime_file, values[1])
    environment = _deployment_environment(
        base_environment,
        build_revision=revision,
        core_port=core_port,
        owner_secret_file=owner_file,
        runtime_secret_file=runtime_file,
    )
    resolved_root = root.resolve()
    return SmokeContext(
        root=resolved_root,
        compose_file=resolved_root / "infra" / "compose.yaml",
        replay_probe=resolved_root / "scripts" / "deployment_replay_probe.py",
        owner_secret_file=owner_file,
        runtime_secret_file=runtime_file,
        secret_values=values,
        environment=environment,
        project_name=project,
        build_revision=revision,
        core_port=core_port,
    )


def wait_for_endpoint(
    base_url: str,
    path: str,
    expected_status: int,
    *,
    timeout_seconds: float = _HTTP_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Poll a loopback endpoint until its exact typed deployment envelope appears."""

    if path not in {"/healthz", "/readyz"}:
        raise SmokeError()
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            with httpx.Client(
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(_HTTP_REQUEST_TIMEOUT_SECONDS),
            ) as client:
                response = client.get(f"{base_url}{path}")
            _validate_endpoint(response, path, expected_status)
            return
        except (httpx.HTTPError, SmokeError, TypeError, ValueError):
            if time.monotonic() >= deadline:
                endpoint = path.removeprefix("/").replace("/", "_")
                raise SmokeError(f"endpoint_{endpoint}_{expected_status}") from None
            sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def exercise_deployment(
    context: SmokeContext,
    runner: CommandRunner,
    *,
    skip_build: bool,
    wait_endpoint: EndpointWaiter | None = None,
) -> None:
    """Run the complete deployment smoke and always destroy its isolated volume."""

    waiter = wait_endpoint or _wait_with_defaults
    primary_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    try:
        _validate_assets(context)
        _exercise_deployment(context, runner, skip_build=skip_build, waiter=waiter)
    except BaseException as error:
        primary_error = error
    try:
        runner.run(
            (
                *context.compose_prefix,
                "down",
                "--volumes",
                "--remove-orphans",
            )
        )
    except BaseException as error:
        cleanup_error = error
    if primary_error is not None:
        if cleanup_error is not None:
            primary_error.add_note("isolated deployment cleanup also failed")
        if isinstance(primary_error, SmokeError):
            raise primary_error
        raise SmokeError() from primary_error
    if cleanup_error is not None:
        raise SmokeError() from cleanup_error


def main(
    argv: Sequence[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-test the hardened paper-only core deployment.",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Reuse the STONKS_BUILD_REVISION image tag instead of building it.",
    )
    arguments = parser.parse_args(argv)
    source = os.environ if environment is None else environment
    try:
        existing_revision = _existing_revision(arguments.skip_build, source)
        with tempfile.TemporaryDirectory(prefix="stonks-core-smoke-") as temporary:
            context = build_context(
                root=ROOT,
                secret_directory=Path(temporary) / "secrets",
                base_environment=source,
                core_port=_free_loopback_port(),
                build_revision=existing_revision,
            )
            runner = SubprocessRunner(
                cwd=context.root,
                environment=context.environment,
                secret_values=context.secret_values,
            )
            exercise_deployment(
                context,
                runner,
                skip_build=arguments.skip_build,
            )
        print(
            json.dumps(
                {
                    "data": {
                        "execution_mode": "paper",
                        "persistence_replay": "verified",
                        "runtime_hardening": "verified",
                    },
                    "error": None,
                    "status": 200,
                    "success": True,
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as error:
        phase = error.phase if isinstance(error, SmokeError) else "unknown"
        print(
            json.dumps(
                {
                    "data": None,
                    "error": {
                        "code": "deployment_smoke_failed",
                        "message": "Core deployment smoke failed",
                        "phase": phase,
                    },
                    "status": 500,
                    "success": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


def _command_phase(command: Sequence[str]) -> str:
    tokens = tuple(command)
    if tokens[:2] != ("docker", "compose"):
        return "command_failed"
    action = next((value for value in tokens if value in _COMPOSE_ACTIONS), "command")
    service = next((value for value in tokens if value in _COMPOSE_SERVICES), "")
    return f"compose_{action}_{service}" if service else f"compose_{action}"


def _exercise_deployment(
    context: SmokeContext,
    runner: CommandRunner,
    *,
    skip_build: bool,
    waiter: EndpointWaiter,
) -> None:
    compose = context.compose_prefix
    if not skip_build:
        runner.run((*compose, "build", "core"))
    runner.run((*compose, "up", "-d", "--wait", "postgres"))
    _run_migration(compose, runner)
    _run_migration(compose, runner)
    runner.run((*compose, "up", "-d", "--wait", "core"))
    waiter(context, "/healthz", 200)
    waiter(context, "/readyz", 200)
    _verify_fake_cycle(compose, runner)
    replay_source = _load_replay_probe(context.replay_probe)
    _run_replay_stage(compose, runner, replay_source, "write")
    runner.run((*compose, "restart", "core"))
    waiter(context, "/readyz", 200)
    runner.run((*compose, "stop", "postgres"))
    waiter(context, "/healthz", 200)
    waiter(context, "/readyz", 503)
    runner.run((*compose, "start", "postgres"))
    runner.run((*compose, "up", "-d", "--wait", "postgres"))
    waiter(context, "/readyz", 200)
    runner.run((*compose, "down", "--remove-orphans"))
    runner.run((*compose, "up", "-d", "--wait", "postgres"))
    _run_migration(compose, runner)
    runner.run((*compose, "up", "-d", "--wait", "core"))
    waiter(context, "/readyz", 200)
    _run_replay_stage(compose, runner, replay_source, "replay")
    _run_replay_stage(compose, runner, replay_source, "verify")
    _verify_runtime_hardening(context, runner)
    _collect_non_secret_evidence(context, runner)


def _run_migration(compose: Sequence[str], runner: CommandRunner) -> None:
    output = runner.run((*compose, "--profile", "migration", "run", "--rm", "migrate"))
    payload = _json_object(output)
    data = payload.get("data")
    if (
        payload.get("success") is not True
        or payload.get("status") != 200
        or not isinstance(data, dict)
        or data.get("status") != "ready"
    ):
        raise SmokeError()


def _verify_fake_cycle(
    compose: Sequence[str],
    runner: CommandRunner,
) -> None:
    command = (
        *compose,
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "stonks",
        "core",
        "fake-cycle",
        "--symbol",
        "AAPL",
        "--as-of",
        "2026-01-02T21:00:00+00:00",
        "--idempotency-key",
        "deployment-smoke-v1",
    )
    first = runner.run(command)
    second = runner.run(command)
    payload = _json_object(first)
    metadata = payload.get("metadata")
    if (
        not first
        or first != second
        or payload.get("success") is not True
        or payload.get("status") != 200
        or not isinstance(metadata, dict)
        or metadata.get("execution_mode") != "paper"
    ):
        raise SmokeError()


def _run_replay_stage(
    compose: Sequence[str],
    runner: CommandRunner,
    source: str,
    stage: str,
) -> None:
    output = runner.run(
        (*compose, "exec", "-T", "core", "python", "-", stage),
        input_text=source,
    )
    payload = _json_object(output)
    expected = {
        "write": ("running", 2),
        "replay": ("succeeded", 3),
        "verify": ("succeeded", 3),
    }
    status, version = expected[stage]
    if (
        payload.get("success") is not True
        or payload.get("stage") != stage
        or payload.get("status") != status
        or payload.get("version") != version
        or payload.get("fresh_inference") is not False
    ):
        raise SmokeError()


def _verify_runtime_hardening(
    context: SmokeContext,
    runner: CommandRunner,
) -> None:
    for service, expected_user in (
        ("core", "65532:65532"),
        ("postgres", "70:70"),
    ):
        container_id = runner.run((*context.compose_prefix, "ps", "-q", service))
        if not container_id or len(container_id.splitlines()) != 1:
            raise SmokeError()
        _verify_container_inspection(
            runner.run(("docker", "inspect", container_id)),
            expected_user=expected_user,
        )
    runner.run(
        (
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            context.image_ref,
            "-c",
            _IMAGE_CONTENT_CHECK,
        )
    )
    runner.run(
        (
            *context.compose_prefix,
            "exec",
            "-T",
            "core",
            "python",
            "-c",
            _READ_ONLY_ROOTFS_CHECK,
        )
    )


def _verify_container_inspection(output: str, *, expected_user: str) -> None:
    raw = json.loads(output)
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise SmokeError()
    config = raw[0].get("Config")
    host = raw[0].get("HostConfig")
    if not isinstance(config, dict) or not isinstance(host, dict):
        raise SmokeError()
    dropped = host.get("CapDrop")
    security = host.get("SecurityOpt")
    if (
        config.get("User") != expected_user
        or host.get("ReadonlyRootfs") is not True
        or host.get("Privileged") is True
        or not isinstance(dropped, list)
        or "ALL" not in dropped
        or not isinstance(security, list)
        or "no-new-privileges:true" not in security
    ):
        raise SmokeError()


def _collect_non_secret_evidence(
    context: SmokeContext,
    runner: CommandRunner,
) -> None:
    runner.run((*context.compose_prefix, "config"))
    runner.run((*context.compose_prefix, "logs", "--no-color"))
    runner.run(("docker", "image", "history", "--no-trunc", context.image_ref))


def _validate_endpoint(
    response: HttpResponse,
    path: str,
    expected_status: int,
) -> None:
    if response.status_code != expected_status:
        raise SmokeError()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("status") != expected_status:
        raise SmokeError()
    data = payload.get("data")
    if path == "/healthz" and expected_status == 200:
        if (
            payload.get("success") is not True
            or not isinstance(data, dict)
            or data.get("execution_mode") != "paper"
            or data.get("status") != "alive"
        ):
            raise SmokeError()
        return
    if path == "/readyz" and expected_status == 200:
        _validate_ready_payload(payload, data)
        return
    error = payload.get("error")
    if (
        path != "/readyz"
        or expected_status != 503
        or payload.get("success") is not False
        or data is not None
        or not isinstance(error, dict)
        or error.get("code") != "data_unavailable"
    ):
        raise SmokeError()


def _validate_ready_payload(payload: Mapping[str, object], data: object) -> None:
    if not isinstance(data, dict):
        raise SmokeError()
    revision = data.get("migration_revision")
    if (
        payload.get("success") is not True
        or data.get("database") is not True
        or data.get("schema_current") is not True
        or data.get("execution_mode") != "paper"
        or not isinstance(revision, str)
        or not revision
        or len(revision) > 64
    ):
        raise SmokeError()


def _wait_with_defaults(
    context: SmokeContext,
    path: str,
    expected_status: int,
) -> None:
    wait_for_endpoint(context.base_url, path, expected_status)


def _load_replay_probe(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise SmokeError()
    size = path.stat().st_size
    if not 1 <= size <= _MAX_REPLAY_PROBE_BYTES:
        raise SmokeError()
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SmokeError() from error


def _validate_assets(context: SmokeContext) -> None:
    if (
        context.compose_file.is_symlink()
        or not context.compose_file.is_file()
        or context.replay_probe.is_symlink()
        or not context.replay_probe.is_file()
        or context.owner_secret_file.is_symlink()
        or not context.owner_secret_file.is_file()
        or context.runtime_secret_file.is_symlink()
        or not context.runtime_secret_file.is_file()
    ):
        raise SmokeError()


def _deployment_environment(
    source: Mapping[str, str],
    *,
    build_revision: str,
    core_port: int,
    owner_secret_file: Path,
    runtime_secret_file: Path,
) -> dict[str, str]:
    environment = {
        key: value
        for key, value in source.items()
        if key.upper() in _SAFE_AMBIENT_ENVIRONMENT
    }
    environment.update(
        {
            "COMPOSE_ANSI": "never",
            "STONKS_BUILD_REVISION": build_revision,
            "STONKS_CORE_PORT": str(core_port),
            "STONKS_ENVIRONMENT": "production",
            "STONKS_POSTGRES_PASSWORD_FILE": str(owner_secret_file),
            "STONKS_RUNTIME_DB_PASSWORD_FILE": str(runtime_secret_file),
        }
    )
    return environment


def _validate_generated_values(
    project_name: str,
    build_revision: str,
    values: tuple[str, str],
) -> None:
    if (
        re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project_name) is None
        or re.fullmatch(r"[0-9a-f]{7,64}", build_revision) is None
        or len(values) != 2
        or values[0] == values[1]
        or any(
            not value
            or len(value) > 256
            or any(character.isspace() or ord(character) < 32 for character in value)
            for value in values
        )
    ):
        raise SmokeError()


def _existing_revision(
    skip_build: bool,
    environment: Mapping[str, str],
) -> str | None:
    if not skip_build:
        return None
    revision = environment.get("STONKS_BUILD_REVISION")
    if revision is None or not revision or revision.strip() != revision:
        raise SmokeError()
    return revision


def _write_secret(path: Path, value: str) -> None:
    try:
        path.write_text(value, encoding="utf-8", newline="\n")
        path.chmod(0o444)
    except OSError as error:
        raise SmokeError() from error


def _reject_secret_output(rendered: str, secret_values: Sequence[str]) -> None:
    if any(secret and secret in rendered for secret in secret_values):
        raise SmokeError()


def _json_object(rendered: str) -> dict[str, Any]:
    try:
        payload = json.loads(rendered)
    except (json.JSONDecodeError, TypeError) as error:
        raise SmokeError() from error
    if not isinstance(payload, dict):
        raise SmokeError()
    return payload


def _free_loopback_port() -> int:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        if not isinstance(port, int) or not 1 <= port <= 65_535:
            raise SmokeError()
        return port
    except OSError as error:
        raise SmokeError() from error


if __name__ == "__main__":
    raise SystemExit(main())
