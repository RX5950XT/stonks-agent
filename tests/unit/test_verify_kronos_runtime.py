from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.verify_kronos_runtime import (
    _alpha_generated_at,
    _compose_ps_command,
    _compose_up_command,
    _network_create_command,
    _start_container,
    _verification_network_name,
)


def test_kronos_runtime_verifier_uses_compose_up_for_published_port() -> None:
    command = _compose_up_command(Path("repo"))

    assert command[-5:] == (
        "up",
        "--build",
        "--detach",
        "--no-deps",
        "kronos-cpu",
    )
    assert "run" not in command
    assert "--build" in command
    assert "--service-ports" not in command


def test_kronos_verifier_attaches_a_unique_non_masquerading_bridge() -> None:
    network = _verification_network_name(
        {"COMPOSE_PROJECT_NAME": "stonks-kronos-verify-123"}
    )

    assert network == "stonks-kronos-verify-123-loopback"
    assert _network_create_command(network) == (
        "docker",
        "network",
        "create",
        "--driver",
        "bridge",
        "--opt",
        "com.docker.network.bridge.enable_ip_masquerade=false",
        network,
    )
    assert _compose_ps_command(Path("repo"))[-3:] == (
        "ps",
        "--quiet",
        "kronos-cpu",
    )


def test_compose_cold_build_timeout_is_reported_without_raw_output() -> None:
    timeout = subprocess.TimeoutExpired(("docker", "compose"), 1_800)

    with (
        patch(
            "scripts.verify_kronos_runtime.subprocess.run",
            side_effect=timeout,
        ),
        pytest.raises(RuntimeError, match=r"compose up exceeded 1800s") as raised,
    ):
        _start_container(Path("repo"), {})

    assert raised.value.__cause__ is timeout


def test_alpha_timestamp_never_precedes_a_worker_stamped_artifact() -> None:
    created = datetime(2026, 8, 20, 4, 2, 18, 297334, tzinfo=UTC)
    host_now = created - timedelta(milliseconds=11)

    assert _alpha_generated_at(created, host_now) == created
    assert _alpha_generated_at(created, created + timedelta(seconds=5)) == (
        created + timedelta(seconds=5)
    )
