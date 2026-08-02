from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stonks_agent.entrypoints.gui import (
    KronosSidecarManager,
    OpenBBSidecarManager,
    app,
    prepare_ephemeral_openbb_runtime,
)

ROOT = Path(__file__).resolve().parents[2]


def test_ephemeral_openbb_runtime_persists_only_public_jwks(tmp_path: Path) -> None:
    runtime = prepare_ephemeral_openbb_runtime(tmp_path / "auth")

    payload = json.loads(runtime.jwks_file.read_text(encoding="utf-8"))
    key = payload["keys"][0]
    assert key["kty"] == "RSA"
    assert key["alg"] == "RS256"
    assert {"d", "p", "q", "dp", "dq", "qi"}.isdisjoint(key)
    assert "PRIVATE KEY" not in runtime.jwks_file.read_text(encoding="utf-8")
    assert runtime.environment["STONKS_SERVICE_OIDC_JWKS_HOST_FILE"] == (
        runtime.jwks_file.as_posix()
    )
    assert "TOKEN" not in " ".join(runtime.environment)
    assert "KEY" not in " ".join(
        name
        for name in runtime.environment
        if name != "STONKS_SERVICE_OIDC_JWKS_HOST_FILE"
    )


def test_sidecar_manager_uses_argv_sanitized_environment_and_cleanup() -> None:
    calls: list[tuple[Sequence[str], Mapping[str, str], bool]] = []

    def runner(
        command: Sequence[str],
        *,
        env: Mapping[str, str],
        shell: bool,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append((command, env, shell))
        return subprocess.CompletedProcess(command, 0, "", "")

    manager = OpenBBSidecarManager(
        root=ROOT,
        environment={
            "STONKS_SERVICE_OIDC_ISSUER": "https://issuer.invalid",
            "STONKS_SERVICE_OIDC_AUDIENCE": "openbb",
        },
        ambient={
            "PATH": "safe-path",
            "ProgramFiles": "safe-program-files",
            "SYSTEMROOT": "safe-root",
            "UNSAFE_AMBIENT_TOKEN": "must-not-propagate",
        },
        runner=runner,
        vcs_ref="a" * 40,
    )

    manager.start()
    manager.stop()

    assert len(calls) == 3
    build, start, stop = calls
    assert build[0][-4:] == (
        "build",
        "--build-arg",
        f"VCS_REF={'a' * 40}",
        "openbb",
    )
    assert start[0][-5:] == (
        "--no-build",
        "--wait",
        "--wait-timeout",
        "240",
        "openbb",
    )
    assert stop[0][-2:] == ("down", "--remove-orphans")
    assert all(shell is False for _, _, shell in calls)
    assert all("UNSAFE_AMBIENT_TOKEN" not in env for _, env, _ in calls)
    assert all(env["PATH"] == "safe-path" for _, env, _ in calls)
    assert all(env["ProgramFiles"] == "safe-program-files" for _, env, _ in calls)


def test_kronos_manager_uses_loopback_bridge_and_never_inherits_llm_secret(
    tmp_path: Path,
) -> None:
    calls: list[tuple[Sequence[str], Mapping[str, str], bool]] = []

    def runner(
        command: Sequence[str],
        *,
        env: Mapping[str, str],
        shell: bool,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append((command, env, shell))
        output = "a" * 64 if "ps" in command else ""
        return subprocess.CompletedProcess(command, 0, output, "")

    model_root = tmp_path / "models"
    model_root.mkdir()
    manager = KronosSidecarManager(
        root=ROOT,
        environment={
            "STONKS_SERVICE_OIDC_ISSUER": "https://issuer.invalid",
            "STONKS_SERVICE_OIDC_CORE_SUBJECT": "service:core",
            "STONKS_SERVICE_OIDC_CORE_CLIENT_ID": "core",
            "STONKS_SERVICE_OIDC_ALGORITHMS": "RS256",
            "STONKS_SERVICE_OIDC_JWKS_HOST_FILE": "C:/safe/jwks.json",
        },
        model_root=model_root,
        port=17_299,
        project_name="stonks-gui-kronos-test",
        ambient={
            "PATH": "safe-path",
            "SYSTEMROOT": "safe-root",
            "STONKS_LLM_API_KEY": "must-not-propagate",
            "HTTP_PROXY": "must-not-propagate",
        },
        runner=runner,
        ready_probe=lambda port: port == 17_299,
    )

    manager.start()
    manager.stop()

    commands = tuple(call[0] for call in calls)
    assert any(command[-2:] == ("build", "kronos-cpu") for command in commands)
    assert any(
        command[-5:]
        == (
            "up",
            "--detach",
            "--no-build",
            "--no-deps",
            "kronos-cpu",
        )
        for command in commands
    )
    assert any(
        tuple(command[:4])
        == (
            "docker",
            "network",
            "create",
            "--driver",
        )
        and "com.docker.network.bridge.enable_ip_masquerade=false" in command
        for command in commands
    )
    assert any(
        tuple(command[:3]) == ("docker", "network", "connect")
        and command[-1] == "a" * 64
        for command in commands
    )
    assert any(command[-2:] == ("down", "--remove-orphans") for command in commands)
    assert any(
        tuple(command[:3]) == ("docker", "network", "rm") for command in commands
    )
    assert all(shell is False for _, _, shell in calls)
    assert all("STONKS_LLM_API_KEY" not in env for _, env, _ in calls)
    assert all("HTTP_PROXY" not in env for _, env, _ in calls)
    assert all(
        env["STONKS_KRONOS_SERVICE_OIDC_AUDIENCE"] == "stonks-gui-kronos"
        for _, env, _ in calls
    )
    assert all(env["STONKS_KRONOS_CPU_PORT"] == "17299" for _, env, _ in calls)


def test_kronos_start_failure_cleans_exact_project_and_network(
    tmp_path: Path,
) -> None:
    commands: list[Sequence[str]] = []

    def runner(
        command: Sequence[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        commands.append(command)
        if tuple(command[:3]) == ("docker", "network", "connect"):
            return subprocess.CompletedProcess(command, 1, "", "denied")
        output = "b" * 64 if "ps" in command else ""
        return subprocess.CompletedProcess(command, 0, output, "")

    models = tmp_path / "models"
    models.mkdir()
    manager = KronosSidecarManager(
        root=ROOT,
        environment={
            "STONKS_SERVICE_OIDC_ISSUER": "https://issuer.invalid",
            "STONKS_SERVICE_OIDC_CORE_SUBJECT": "service:core",
            "STONKS_SERVICE_OIDC_CORE_CLIENT_ID": "core",
            "STONKS_SERVICE_OIDC_ALGORITHMS": "RS256",
            "STONKS_SERVICE_OIDC_JWKS_HOST_FILE": "C:/safe/jwks.json",
        },
        model_root=models,
        port=17_298,
        project_name="stonks-gui-kronos-failure",
        runner=runner,
        ready_probe=lambda port: True,
    )

    with pytest.raises(RuntimeError, match="lifecycle"):
        manager.start()

    assert any(command[-2:] == ("down", "--remove-orphans") for command in commands)
    assert any(
        tuple(command[:3]) == ("docker", "network", "rm") for command in commands
    )


def test_gui_cli_help_is_available() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "serve" in result.stdout
    assert "OpenBB" in result.stdout


def test_gui_cli_fails_clearly_outside_source_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(app, ["serve", "--no-open-browser"])

    assert result.exit_code == 1
    assert "configuration_invalid" in result.stderr
    assert "source checkout" in result.stderr
