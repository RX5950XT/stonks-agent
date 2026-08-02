from __future__ import annotations

from pathlib import Path

from scripts.verify_kronos_runtime import (
    _compose_ps_command,
    _compose_up_command,
    _network_create_command,
    _verification_network_name,
)


def test_kronos_runtime_verifier_uses_compose_up_for_published_port() -> None:
    command = _compose_up_command(Path("repo"))

    assert command[-4:] == (
        "up",
        "--detach",
        "--no-deps",
        "kronos-cpu",
    )
    assert "run" not in command
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
