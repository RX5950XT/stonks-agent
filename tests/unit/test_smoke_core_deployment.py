from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts import smoke_core_deployment
from scripts.smoke_core_deployment import (
    CommandRunner,
    SmokeContext,
    SmokeError,
    SubprocessRunner,
    build_context,
    exercise_deployment,
    wait_for_endpoint,
)


class FakeRunner(CommandRunner):
    def __init__(self, *, fail_at: tuple[str, ...] | None = None) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.inputs: list[str | None] = []
        self._fail_at = fail_at

    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> str:
        normalized = tuple(command)
        self.commands.append(normalized)
        self.inputs.append(input_text)
        if self._fail_at is not None and self._fail_at == normalized:
            raise SmokeError()
        if normalized[-3:] == ("ps", "-q", "core"):
            return "core-container"
        if normalized[-3:] == ("ps", "-q", "postgres"):
            return "postgres-container"
        if normalized[:2] == ("docker", "inspect"):
            expected_user = (
                "65532:65532" if normalized[-1] == "core-container" else "70:70"
            )
            return json.dumps(
                [
                    {
                        "Config": {"User": expected_user},
                        "HostConfig": {
                            "CapDrop": ["ALL"],
                            "ReadonlyRootfs": True,
                            "SecurityOpt": ["no-new-privileges:true"],
                        },
                    }
                ]
            )
        if normalized[-4:] == (
            "migration",
            "run",
            "--rm",
            "migrate",
        ):
            return json.dumps(
                {
                    "success": True,
                    "status": 200,
                    "data": {"status": "ready"},
                }
            )
        if "fake-cycle" in normalized:
            return json.dumps(
                {
                    "success": True,
                    "status": 200,
                    "metadata": {"execution_mode": "paper"},
                },
                sort_keys=True,
            )
        if normalized[-6:-2] == ("exec", "-T", "core", "python"):
            stage = normalized[-1]
            status = "running" if stage == "write" else "succeeded"
            version = 2 if stage == "write" else 3
            return json.dumps(
                {
                    "fresh_inference": False,
                    "stage": stage,
                    "status": status,
                    "success": True,
                    "version": version,
                }
            )
        return ""


def _context(tmp_path: Path) -> SmokeContext:
    root = tmp_path / "repo"
    (root / "infra").mkdir(parents=True)
    (root / "infra" / "compose.yaml").write_text("services: {}", encoding="utf-8")
    (root / "scripts").mkdir()
    replay = root / "scripts" / "deployment_replay_probe.py"
    replay.write_text("print('probe')\n", encoding="utf-8")
    return build_context(
        root=root,
        secret_directory=tmp_path / "secrets",
        base_environment={"PATH": "safe"},
        core_port=18_321,
        project_name="stonks-smoke-test",
        build_revision="deadbeef",
        secret_values=("owner-secret", "runtime-secret"),
    )


def _wait_recorder(
    calls: list[tuple[str, int]],
) -> Callable[[SmokeContext, str, int], None]:
    def wait(context: SmokeContext, path: str, status: int) -> None:
        assert context.core_port == 18_321
        calls.append((path, status))

    return wait


def test_build_context_writes_private_secret_files_without_secret_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = iter(("owner-generated", "runtime-generated"))
    monkeypatch.setattr(
        smoke_core_deployment.secrets,
        "token_urlsafe",
        lambda _: next(generated),
    )
    monkeypatch.setattr(
        smoke_core_deployment.secrets,
        "token_hex",
        lambda _: "abc1234",
    )

    context = build_context(
        root=tmp_path,
        secret_directory=tmp_path / "secrets",
        base_environment={
            "PATH": "safe",
            "ProgramFiles": "C:/Program Files",
            "UNSAFE_AMBIENT_TOKEN": "must-not-propagate",
        },
        core_port=18_000,
    )

    assert context.project_name == "stonks-smoke-abc1234"
    assert context.build_revision == "abc1234"
    assert context.owner_secret_file.read_text(encoding="utf-8") == "owner-generated"
    assert (
        context.runtime_secret_file.read_text(encoding="utf-8") == "runtime-generated"
    )
    assert context.environment["STONKS_CORE_PORT"] == "18000"
    assert context.environment["STONKS_POSTGRES_PASSWORD_FILE"] == str(
        context.owner_secret_file
    )
    assert context.environment["STONKS_RUNTIME_DB_PASSWORD_FILE"] == str(
        context.runtime_secret_file
    )
    assert "owner-generated" not in context.environment.values()
    assert "runtime-generated" not in context.environment.values()
    assert context.environment["ProgramFiles"] == "C:/Program Files"
    assert "UNSAFE_AMBIENT_TOKEN" not in context.environment


def test_subprocess_runner_uses_argv_and_never_renders_failed_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def failed_run(
        command: Sequence[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        observed["command"] = tuple(command)
        observed.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="database detail",
            stderr="runtime-secret",
        )

    monkeypatch.setattr(smoke_core_deployment.subprocess, "run", failed_run)
    runner = SubprocessRunner(
        cwd=Path("C:/repo"),
        environment={"PATH": "safe"},
        secret_values=("runtime-secret",),
    )

    with pytest.raises(SmokeError) as raised:
        runner.run(("docker", "compose", "config"))

    assert str(raised.value) == "Core deployment smoke failed"
    assert observed["command"] == ("docker", "compose", "config")
    assert observed["shell"] is False
    assert observed["capture_output"] is True
    assert observed["env"] == {"PATH": "safe"}
    assert "runtime-secret" not in str(raised.value)
    assert "database detail" not in str(raised.value)


def test_subprocess_runner_rejects_secret_in_successful_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        smoke_core_deployment.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout="owner-secret",
            stderr="",
        ),
    )
    runner = SubprocessRunner(
        cwd=Path("C:/repo"),
        environment={},
        secret_values=("owner-secret",),
    )

    with pytest.raises(SmokeError):
        runner.run(("docker", "compose", "logs"))


def test_wait_for_endpoint_uses_exact_safe_envelopes_and_no_ambient_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = {
        "/healthz": (
            200,
            {
                "success": True,
                "status": 200,
                "data": {
                    "build_revision": "smoke-test",
                    "execution_mode": "paper",
                    "status": "alive",
                },
                "error": None,
            },
        ),
        "/readyz": (
            200,
            {
                "success": True,
                "status": 200,
                "data": {
                    "database": True,
                    "schema_current": True,
                    "execution_mode": "paper",
                    "migration_revision": "0017",
                },
                "error": None,
            },
        ),
        "/unreadyz": (
            503,
            {
                "success": False,
                "status": 503,
                "data": None,
                "error": {"code": "data_unavailable"},
            },
        ),
    }
    clients: list[dict[str, object]] = []

    class Response:
        def __init__(self, status_code: int, payload: dict[str, object]) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    class Client:
        def __init__(self, **kwargs: object) -> None:
            clients.append(kwargs)

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str) -> Response:
            path = "/" + url.rsplit("/", maxsplit=1)[-1]
            status, payload = payloads[path]
            return Response(status, payload)

    monkeypatch.setattr(smoke_core_deployment.httpx, "Client", Client)

    wait_for_endpoint(
        "http://127.0.0.1:18000",
        "/healthz",
        200,
        timeout_seconds=1,
        sleep=lambda _: None,
    )
    wait_for_endpoint(
        "http://127.0.0.1:18000",
        "/readyz",
        200,
        timeout_seconds=1,
        sleep=lambda _: None,
    )
    payloads["/readyz"] = payloads["/unreadyz"]
    wait_for_endpoint(
        "http://127.0.0.1:18000",
        "/readyz",
        503,
        timeout_seconds=1,
        sleep=lambda _: None,
    )

    assert clients
    assert all(client["trust_env"] is False for client in clients)
    assert all(client["follow_redirects"] is False for client in clients)


def test_wait_for_endpoint_fails_closed_on_wrong_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(
        200,
        json={"success": True, "status": 200, "data": {"status": "alive"}},
    )

    class Client:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str) -> httpx.Response:
            del url
            return response

    monkeypatch.setattr(smoke_core_deployment.httpx, "Client", Client)

    with pytest.raises(SmokeError):
        wait_for_endpoint(
            "http://127.0.0.1:18000",
            "/healthz",
            200,
            timeout_seconds=0,
            sleep=lambda _: None,
        )


def test_exercise_runs_full_persistence_security_and_cleanup_flow(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    runner = FakeRunner()
    waits: list[tuple[str, int]] = []

    exercise_deployment(
        context,
        runner,
        skip_build=False,
        wait_endpoint=_wait_recorder(waits),
    )

    compose = context.compose_prefix
    assert (*compose, "build", "core") in runner.commands
    assert (
        runner.commands.count(
            (*compose, "--profile", "migration", "run", "--rm", "migrate")
        )
        == 3
    )
    assert (
        runner.commands.count(
            (
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
        )
        == 2
    )
    assert waits == [
        ("/healthz", 200),
        ("/readyz", 200),
        ("/readyz", 200),
        ("/healthz", 200),
        ("/readyz", 503),
        ("/readyz", 200),
        ("/readyz", 200),
    ]
    replay_commands = [
        command
        for command in runner.commands
        if command[-6:-2] == ("exec", "-T", "core", "python") and command[-2] == "-"
    ]
    assert [command[-1] for command in replay_commands] == [
        "write",
        "replay",
        "verify",
    ]
    assert all(value == "print('probe')\n" for value in runner.inputs if value)
    assert (*compose, "down", "--remove-orphans") in runner.commands
    assert runner.commands[-1] == (
        *compose,
        "down",
        "--volumes",
        "--remove-orphans",
    )
    assert ("docker", "image", "history", "--no-trunc", context.image_ref) in (
        runner.commands
    )
    image_checks = [
        command
        for command in runner.commands
        if command[:5] == ("docker", "run", "--rm", "--entrypoint", "python")
    ]
    assert len(image_checks) == 1
    rootfs_checks = [
        command
        for command in runner.commands
        if command[: len(compose) + 4] == (*compose, "exec", "-T", "core", "python")
        and "write-probe" in command[-1]
    ]
    assert len(rootfs_checks) == 1


def test_exercise_skips_build_but_always_cleans_up_after_failure(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    failed = (*context.compose_prefix, "up", "-d", "--wait", "postgres")
    runner = FakeRunner(fail_at=failed)

    with pytest.raises(SmokeError):
        exercise_deployment(
            context,
            runner,
            skip_build=True,
            wait_endpoint=lambda *args: None,
        )

    assert (*context.compose_prefix, "build", "core") not in runner.commands
    assert runner.commands[-1] == (
        *context.compose_prefix,
        "down",
        "--volumes",
        "--remove-orphans",
    )
