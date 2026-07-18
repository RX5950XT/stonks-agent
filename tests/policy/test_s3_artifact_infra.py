from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "infra" / "compose.artifacts.yaml"
SMOKE = ROOT / "scripts" / "smoke_s3_artifacts.py"
IMAGE = (
    "chrislusf/seaweedfs:4.34@"
    "sha256:6620371e8af8282056685c652d4637265698c9e2c2d59f9594e6ac333ad6c634"
)


def manifest() -> dict[str, object]:
    value = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def service() -> dict[str, object]:
    services = manifest()["services"]
    assert isinstance(services, dict)
    value = services["s3-compatible"]
    assert isinstance(value, dict)
    return value


def test_s3_compatible_image_and_source_identity_are_exact() -> None:
    value = service()

    assert value["image"] == IMAGE
    assert value["command"] == [
        "mini",
        "-dir=/data",
        "-master.telemetry=false",
        "-admin.ui=false",
        "-webdav=false",
    ]
    assert "latest" not in str(value["image"])


def test_s3_compatible_runtime_is_loopback_nonroot_readonly_and_bounded() -> None:
    value = service()
    rendered = json.dumps(value, sort_keys=True)

    assert value["user"] == "65532:65532"
    assert value["read_only"] is True
    assert value["cap_drop"] == ["ALL"]
    assert value["security_opt"] == ["no-new-privileges:true"]
    assert value["pids_limit"] == 128
    assert value["mem_limit"] == "384m"
    assert value["cpus"] == 1.0
    assert value["ports"] == ["127.0.0.1:${STONKS_S3_TEST_PORT:-18333}:8333"]
    assert "/data:rw,noexec,nosuid,nodev" in rendered
    assert "-master.telemetry=false" in rendered
    assert "host" not in manifest().get("networks", {})


def test_s3_test_credentials_are_runtime_inputs_not_committed_values() -> None:
    environment = service()["environment"]
    assert isinstance(environment, dict)

    assert environment == {
        "AWS_ACCESS_KEY_ID": "${STONKS_S3_TEST_ACCESS_KEY:?required}",
        "AWS_SECRET_ACCESS_KEY": "${STONKS_S3_TEST_SECRET_KEY:?required}",
        "S3_BUCKET": "stonks-artifacts",
    }
    content = COMPOSE.read_text(encoding="utf-8")
    assert "stonkstestaccess" not in content
    assert "stonkstestsecret" not in content


def test_s3_compatible_compose_renders_with_ephemeral_credentials() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    result = subprocess.run(
        [docker, "compose", "-f", str(COMPOSE), "config", "--quiet"],
        cwd=ROOT,
        env=_environment(_free_port()),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_pinned_s3_compatible_runtime_smoke_when_image_is_local() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    if subprocess.run(
        [docker, "image", "inspect", IMAGE],
        check=False,
        capture_output=True,
        timeout=15,
    ).returncode:
        pytest.skip("pinned S3-compatible image is not present locally")
    port = _free_port()
    environment = _environment(port)
    project = f"stonks-s3-smoke-{os.getpid()}"
    command = [docker, "compose", "-p", project, "-f", str(COMPOSE)]
    try:
        started = subprocess.run(
            [*command, "up", "--detach", "--wait", "--pull", "never"],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert started.returncode == 0, started.stderr
        smoke = subprocess.run(
            [sys.executable, str(SMOKE)],
            cwd=ROOT,
            env={
                **environment,
                "STONKS_S3_TEST_ENDPOINT": f"http://127.0.0.1:{port}",
            },
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert smoke.returncode == 0, smoke.stderr
        result = json.loads(smoke.stdout)
        assert result["runtime"] == "seaweedfs-4.34"
        assert result["hash_round_trip"] == "verified"
        assert result["presigned_get"] == "verified"
    finally:
        subprocess.run(
            [*command, "down", "--volumes", "--remove-orphans"],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            timeout=30,
        )


def _environment(port: int) -> dict[str, str]:
    return {
        **os.environ,
        "STONKS_S3_TEST_ACCESS_KEY": f"stonks{secrets.token_hex(8)}",
        "STONKS_S3_TEST_SECRET_KEY": secrets.token_urlsafe(32),
        "STONKS_S3_TEST_PORT": str(port),
    }


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
