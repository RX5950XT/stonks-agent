#!/usr/bin/env python3
"""Exercise two fresh hardened RD factor sandboxes and failure fixtures."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml

from stonks_agent.application.evaluation.rd_agent import aggregate_sandbox_runs
from stonks_agent.domain.errors import Success
from stonks_contracts.rd_agent import (
    CandidateSandboxPolicy,
    RDAgentCandidateKind,
    RDAgentProposal,
    RDSandboxDataset,
    RDSandboxDatasetRow,
    RDSandboxInvocation,
    RDSandboxJob,
    RDSandboxRunResponse,
    RDSandboxRuntimeIdentity,
)
from workers.quant_lab.rd_agent.adapter import compute_runtime_hash

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "workers" / "quant_lab" / "rd_agent"
IMAGE = "stonks-rd-agent-factor-sandbox:p5.8"
COMMIT = "4f9ecb005881cddc08df0124a2e894c018007679"
SOURCE_HASH = "837010068449c5e874dee83f91240b281bed529acd214fdd1bdb6678f620caa4"
SAFE_SOURCE = """def compute(rows):
    return [
        {"observation_id": row["observation_id"], "predicted_return": row["features"][0]}
        for row in rows
    ]
"""
CPU_BOMB = "def compute(rows):\n    return sum(range(1000000000000))\n"
OUTPUT_BOMB = """def compute(rows):
    return [
        {"observation_id": row["observation_id"], "predicted_return": row["features"][0]}
        for row in rows
    ] * 1000000
"""
ESCAPE_SOURCE = "import socket\ndef compute(rows):\n    return rows\n"


def main() -> int:
    policy = _policy()
    runtime = _runtime_identity()
    safe_job = _job(runtime, policy, SAFE_SOURCE)
    first, first_container = _run_once(safe_job, runtime, policy)
    replay, replay_container = _run_once(safe_job, runtime, policy)
    combined = aggregate_sandbox_runs(
        job=safe_job,
        first=first,
        replay=replay,
        sandbox_policy=policy,
    )
    if not isinstance(combined, Success):
        raise RuntimeError("fresh sandbox aggregation failed")
    rejected = _run_failure(_job(runtime, policy, ESCAPE_SOURCE), runtime, policy)
    timed_out = _run_failure(_job(runtime, policy, CPU_BOMB), runtime, policy)
    bounded = _run_failure(_job(runtime, policy, OUTPUT_BOMB), runtime, policy)
    _probe_container_boundary(policy)
    _probe_removed_capabilities(policy)
    summary = {
        "runtime_hash": runtime.runtime_hash,
        "image_digest": runtime.image_digest,
        "first_container_id": first_container,
        "replay_container_id": replay_container,
        "first_instance_id": str(first.result.sandbox_instance_id),
        "replay_instance_id": str(replay.result.sandbox_instance_id),
        "prediction_hash": combined.value.result.first_output_hash,
        "scan_escape": rejected,
        "cpu_bomb": timed_out,
        "output_bomb": bounded,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _policy() -> CandidateSandboxPolicy:
    payload: Any = yaml.safe_load(
        (WORKER / "sandbox_policy.yaml").read_text(encoding="utf-8")
    )
    return CandidateSandboxPolicy.model_validate(payload["sandbox"])


def _runtime_identity() -> RDSandboxRuntimeIdentity:
    digest = _docker("image", "inspect", IMAGE, "--format", "{{.Id}}")
    version = _docker(
        "run", "--rm", "--entrypoint", "python", IMAGE, "--version"
    ).removeprefix("Python ")
    return RDSandboxRuntimeIdentity(
        worker_version="rd-agent-factor-sandbox/0.1.0",
        adapter_version="factor-expression-v1",
        rd_agent_commit=COMMIT,
        rd_agent_source_hash=SOURCE_HASH,
        runtime_hash=compute_runtime_hash(WORKER),
        image_digest=digest,
        python_version=version,
        deterministic=True,
    )


def _job(
    runtime: RDSandboxRuntimeIdentity,
    policy: CandidateSandboxPolicy,
    source: str,
) -> RDSandboxJob:
    now = datetime.now(UTC)
    data = _dataset(now - timedelta(minutes=1))
    proposal = RDAgentProposal.create(
        proposal_id=UUID("70000000-0000-4000-8000-000000000001"),
        candidate_id="rd-factor-smoke/1.0.0",
        candidate_kind=RDAgentCandidateKind.FACTOR,
        rd_agent_commit=COMMIT,
        generation_runtime_hash="a" * 64,
        generation_config_hash="b" * 64,
        generation_input_artifact_ref="sha256:" + "c" * 64,
        raw_generation_artifact_ref="sha256:" + "d" * 64,
        source=source,
        generated_at=now - timedelta(seconds=1),
    )
    return RDSandboxJob(
        request_id=UUID("70000000-0000-4000-8000-000000000002"),
        run_id=UUID("70000000-0000-4000-8000-000000000003"),
        job_id=UUID("70000000-0000-4000-8000-000000000004"),
        attempt_generation=1,
        attempt_nonce="rd-agent-smoke-attempt-1",
        execution_mode="sandbox",
        proposal=proposal,
        dataset_artifact_ref=f"sha256:{data.payload_hash()}",
        dataset=data,
        evaluation_policy_hash="e" * 64,
        sandbox_policy_hash=policy.policy_hash,
        runtime=runtime,
        requested_at=now,
        deadline=now + timedelta(minutes=2),
        promotion_allowed=False,
    )


def _dataset(as_of: datetime) -> RDSandboxDataset:
    instrument = UUID("70000000-0000-4000-8000-000000000005")
    return RDSandboxDataset(
        dataset_snapshot_id=UUID("70000000-0000-4000-8000-000000000006"),
        source_data_hash="a" * 64,
        as_of=as_of,
        feature_spec_hash="b" * 64,
        label_spec_hash="c" * 64,
        universe_spec_hash="d" * 64,
        cost_model_hash="e" * 64,
        split_policy_hash="f" * 64,
        rows=tuple(
            RDSandboxDatasetRow(
                observation_id=UUID(f"70000000-0000-4000-9000-{index:012d}"),
                instrument_id=instrument,
                event_at=as_of - timedelta(days=5 - index),
                feature_available_at=as_of - timedelta(days=5 - index, minutes=-1),
                prediction_at=as_of - timedelta(days=4 - index),
                features=(Decimal(f"0.0{index}"), Decimal("1")),
            )
            for index in range(1, 4)
        ),
    )


def _run_once(
    job: RDSandboxJob,
    runtime: RDSandboxRuntimeIdentity,
    policy: CandidateSandboxPolicy,
) -> tuple[RDSandboxRunResponse, str]:
    container_id = _docker(*_create_args(runtime, policy))
    try:
        _assert_inspect(container_id, runtime, policy)
        instance_id = uuid5(NAMESPACE_URL, f"docker:{container_id}")
        invocation = RDSandboxInvocation(
            sandbox_instance_id=instance_id,
            job=job,
        )
        output = _docker_input(
            ("start", "--attach", "--interactive", container_id),
            invocation.canonical_json().encode("utf-8"),
        )
        envelope = json.loads(output)
        if envelope.get("status") != 200 or envelope.get("success") is not True:
            raise RuntimeError(f"sandbox failed: {envelope.get('error')}")
        response = RDSandboxRunResponse.model_validate(envelope["data"])
        response.validate_against(job)
        return response, container_id
    finally:
        _docker("rm", "--force", container_id)


def _run_failure(
    job: RDSandboxJob,
    runtime: RDSandboxRuntimeIdentity,
    policy: CandidateSandboxPolicy,
) -> str:
    container_id = _docker(*_create_args(runtime, policy))
    try:
        invocation = RDSandboxInvocation(
            sandbox_instance_id=uuid5(NAMESPACE_URL, f"docker:{container_id}"),
            job=job,
        )
        output = _docker_input(
            ("start", "--attach", "--interactive", container_id),
            invocation.canonical_json().encode("utf-8"),
        )
        envelope = json.loads(output)
        if envelope.get("success") is not False:
            raise RuntimeError("malicious candidate unexpectedly succeeded")
        return str(envelope["error"]["code"])
    finally:
        _docker("rm", "--force", container_id)


def _create_args(
    runtime: RDSandboxRuntimeIdentity,
    policy: CandidateSandboxPolicy,
) -> tuple[str, ...]:
    return (
        "create",
        "--interactive",
        "--network",
        "none",
        "--read-only",
        "--user",
        f"{policy.run_as_uid}:{policy.run_as_gid}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--security-opt",
        "apparmor=docker-default",
        "--ipc",
        "private",
        "--pids-limit",
        "16",
        "--memory",
        f"{policy.memory_megabytes}m",
        "--cpus",
        str(policy.cpu_cores),
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,nodev,size={policy.writable_tmpfs_megabytes}m,uid=65532,gid=65532,mode=0700",
        "--env",
        f"STONKS_RD_RUNTIME_HASH={runtime.runtime_hash}",
        "--env",
        f"STONKS_RD_IMAGE_DIGEST={runtime.image_digest}",
        IMAGE,
    )


def _assert_inspect(
    container_id: str,
    runtime: RDSandboxRuntimeIdentity,
    policy: CandidateSandboxPolicy,
) -> None:
    inspect = json.loads(_docker("inspect", container_id))[0]
    host = inspect["HostConfig"]
    config = inspect["Config"]
    assert config["User"] == f"{policy.run_as_uid}:{policy.run_as_gid}"
    assert host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"] is True and host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"]
    assert "no-new-privileges" in host["SecurityOpt"]
    assert "apparmor=docker-default" in host["SecurityOpt"]
    assert host["IpcMode"] == "private"
    assert host["PidsLimit"] == 16
    assert host["Memory"] == policy.memory_megabytes * 1024 * 1024
    assert host["NanoCpus"] == int(policy.cpu_cores * Decimal(1_000_000_000))
    assert not host["Binds"] and not host["Devices"]
    assert inspect["Mounts"] == []
    environment = tuple(config["Env"])
    assert f"STONKS_RD_RUNTIME_HASH={runtime.runtime_hash}" in environment
    assert f"STONKS_RD_IMAGE_DIGEST={runtime.image_digest}" in environment
    assert not any(
        token in value.lower()
        for value in environment
        for token in ("password=", "secret=", "token=", "credential=")
    )


def _probe_container_boundary(policy: CandidateSandboxPolicy) -> None:
    code = """
import os, socket, sys
failures = []
for target in [('1.1.1.1', 53), ('169.254.169.254', 80), ('::1', 9)]:
    try:
        socket.create_connection(target, timeout=0.5)
        failures.append('network')
    except OSError:
        pass
try:
    open('/workspace/forbidden-write', 'w').write('x')
    failures.append('root-write')
except OSError:
    pass
if os.path.exists('/var/run/docker.sock'):
    failures.append('docker-socket')
sys.exit(1 if failures else 0)
"""
    command = (
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        f"{policy.run_as_uid}:{policy.run_as_gid}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "16",
        "--memory",
        f"{policy.memory_megabytes}m",
        "--cpus",
        str(policy.cpu_cores),
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=32m,uid=65532,gid=65532,mode=0700",
        "--entrypoint",
        "python",
        IMAGE,
        "-I",
        "-S",
        "-c",
        code,
    )
    _docker(*command)


def _probe_removed_capabilities(policy: CandidateSandboxPolicy) -> None:
    code = """
import importlib, sys
modules = (
    'asyncio.windows_events',
    'bz2',
    'gzip',
    'html.parser',
    'http.cookies',
    'lzma',
    'sqlite3',
    'tarfile',
    'webbrowser',
    'xml.parsers.expat',
    'xml.etree.ElementTree',
)
present = []
for module in modules:
    try:
        importlib.import_module(module)
    except (ImportError, ModuleNotFoundError):
        pass
    else:
        present.append(module)
sys.exit(1 if present else 0)
"""
    _docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        f"{policy.run_as_uid}:{policy.run_as_gid}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--entrypoint",
        "python",
        IMAGE,
        "-I",
        "-S",
        "-c",
        code,
    )
    _docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        f"{policy.run_as_uid}:{policy.run_as_gid}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--entrypoint",
        "sh",
        IMAGE,
        "-c",
        "! apk info -e sqlite-libs && ! test -e /usr/local/bin/pip",
    )


def _docker(*arguments: str) -> str:
    completed = subprocess.run(
        ("docker", *arguments),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return (completed.stdout or completed.stderr).strip()


def _docker_input(arguments: tuple[str, ...], body: bytes) -> str:
    completed = subprocess.run(
        ("docker", *arguments),
        cwd=ROOT,
        input=body,
        check=True,
        capture_output=True,
        timeout=180,
    )
    return completed.stdout.decode("utf-8").strip()


if __name__ == "__main__":
    raise SystemExit(main())
