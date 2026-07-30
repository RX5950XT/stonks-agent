"""Bounded local Docker lifecycle for the authenticated Kronos CPU worker."""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import httpx

type Runner = Callable[..., subprocess.CompletedProcess[str]]
_KRONOS_AUDIENCE = "stonks-gui-kronos"
_SAFE_AMBIENT_ENV = frozenset(
    {
        "APPDATA",
        "COMPOSE_PARALLEL_LIMIT",
        "COMPOSE_PROGRESS",
        "COMSPEC",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
        "XDG_RUNTIME_DIR",
    }
)


class KronosSidecarManager:
    """Own one exact Compose project and no-masquerade loopback bridge."""

    def __init__(
        self,
        *,
        root: Path,
        environment: Mapping[str, str],
        model_root: Path,
        port: int,
        project_name: str | None = None,
        ambient: Mapping[str, str] | None = None,
        runner: Runner = subprocess.run,
        ready_probe: Callable[[int], bool] | None = None,
    ) -> None:
        selected_project = project_name or (
            f"stonks-gui-kronos-{os.getpid()}-{uuid4().hex[:8]}"
        )
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,95}", selected_project) is None:
            raise RuntimeError("Kronos Compose project is invalid")
        if not 1_024 <= port <= 65_535:
            raise RuntimeError("Kronos port is invalid")
        selected_model_root = model_root.resolve(strict=True)
        if selected_model_root.is_symlink() or not selected_model_root.is_dir():
            raise RuntimeError("Kronos model root is invalid")
        required = _service_environment(environment)
        self._root = root.resolve()
        self._environment = {
            **required,
            "COMPOSE_PROJECT_NAME": selected_project,
            "STONKS_KRONOS_CPU_PORT": str(port),
            "STONKS_KRONOS_MODEL_ROOT": selected_model_root.as_posix(),
            "STONKS_KRONOS_SERVICE_OIDC_AUDIENCE": _KRONOS_AUDIENCE,
        }
        self._ambient = dict(os.environ if ambient is None else ambient)
        self._runner = runner
        self._project_name = selected_project
        self._network_name = f"{selected_project}-loopback"
        self._port = port
        self._ready_probe = ready_probe or _wait_until_ready

    def start(self) -> None:
        try:
            self._run((*self._prefix(), "build", "kronos-cpu"), timeout=1_800)
            self._run(
                (
                    *self._prefix(),
                    "up",
                    "--detach",
                    "--no-build",
                    "--no-deps",
                    "kronos-cpu",
                ),
                timeout=300,
            )
            self._create_bridge()
            self._run(
                (
                    "docker",
                    "network",
                    "connect",
                    self._network_name,
                    self._container_id(),
                ),
                timeout=30,
            )
            if not self._ready_probe(self._port):
                raise RuntimeError("Kronos readiness failed")
        except Exception:
            with suppress(Exception):
                self.stop()
            raise

    def stop(self) -> None:
        failed = False
        for command, timeout in (
            ((*self._prefix(), "down", "--remove-orphans"), 120),
            (("docker", "network", "rm", self._network_name), 30),
        ):
            try:
                self._run(command, timeout=timeout)
            except Exception:
                failed = True
        if failed:
            raise RuntimeError("Kronos sidecar cleanup failed")

    def _create_bridge(self) -> None:
        self._run(
            (
                "docker",
                "network",
                "create",
                "--driver",
                "bridge",
                "--opt",
                "com.docker.network.bridge.enable_ip_masquerade=false",
                self._network_name,
            ),
            timeout=30,
        )

    def _container_id(self) -> str:
        result = self._run_result(
            (*self._prefix(), "ps", "--quiet", "kronos-cpu"),
            timeout=30,
        )
        value = result.stdout.strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise RuntimeError("Kronos container identity is invalid")
        return value

    def _prefix(self) -> tuple[str, ...]:
        compose = self._root / "infra" / "compose.kronos.yaml"
        if not compose.is_file() or compose.is_symlink():
            raise RuntimeError("Kronos Compose manifest is unavailable")
        return (
            "docker",
            "compose",
            "-p",
            self._project_name,
            "-f",
            str(compose),
        )

    def _run(self, command: Sequence[str], *, timeout: int) -> None:
        self._run_result(command, timeout=timeout)

    def _run_result(
        self,
        command: Sequence[str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        result = self._runner(
            tuple(command),
            cwd=self._root,
            env=self._safe_environment(),
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("Kronos sidecar lifecycle failed")
        return result

    def _safe_environment(self) -> dict[str, str]:
        safe = {
            name: value
            for name, value in self._ambient.items()
            if name.upper() in _SAFE_AMBIENT_ENV and value
        }
        safe.update(self._environment)
        return safe


def _service_environment(environment: Mapping[str, str]) -> dict[str, str]:
    values = {
        name: environment.get(name, "")
        for name in (
            "STONKS_SERVICE_OIDC_ISSUER",
            "STONKS_SERVICE_OIDC_CORE_SUBJECT",
            "STONKS_SERVICE_OIDC_CORE_CLIENT_ID",
            "STONKS_SERVICE_OIDC_ALGORITHMS",
            "STONKS_SERVICE_OIDC_JWKS_HOST_FILE",
        )
    }
    if not all(values.values()):
        raise RuntimeError("Kronos service identity is incomplete")
    return values


def _wait_until_ready(port: int) -> bool:
    deadline = time.monotonic() + 600
    with httpx.Client(
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(2),
    ) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(f"http://127.0.0.1:{port}/readyz")
                payload = response.json()
                if (
                    response.status_code == 200
                    and isinstance(payload, dict)
                    and payload.get("success") is True
                    and payload.get("data") == {"ready": True}
                ):
                    return True
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.5)
    return False
