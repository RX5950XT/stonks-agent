"""One-shot JSON CLI for a single fresh sandbox invocation."""

from __future__ import annotations

import json
import os
import platform
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stonks_contracts.rd_agent import (
    CandidateSandboxPolicy,
    RDSandboxInvocation,
    RDSandboxRuntimeIdentity,
)
from workers.quant_lab.rd_agent.adapter import (
    CandidateProcessRunner,
    RDAgentSandboxWorker,
    SandboxWorkerPolicy,
    WorkerFailure,
    compute_runtime_hash,
)
from workers.quant_lab.rd_agent.runner import PythonCandidateRunner


class RuntimeTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_version: str = Field(min_length=1, max_length=128)
    adapter_version: str = Field(min_length=1, max_length=128)
    rd_agent_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    rd_agent_source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    python_version: str = Field(min_length=1, max_length=64)


class SettingsFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_request_bytes: int = Field(ge=1, le=16_777_216)
    runtime: RuntimeTemplate
    sandbox: CandidateSandboxPolicy


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_request_bytes: int = Field(ge=1, le=16_777_216)
    runtime: RDSandboxRuntimeIdentity
    sandbox: CandidateSandboxPolicy


def load_settings(worker_root: Path) -> RuntimeSettings:
    try:
        payload: Any = yaml.safe_load(
            (worker_root / "sandbox_policy.yaml").read_text(encoding="utf-8")
        )
        source = SettingsFile.model_validate(payload)
    except (OSError, TypeError, yaml.YAMLError, ValidationError) as error:
        raise RuntimeError("RD sandbox policy is invalid") from error
    calculated = compute_runtime_hash(worker_root)
    if _required("STONKS_RD_RUNTIME_HASH") != calculated:
        raise RuntimeError("RD sandbox runtime hash mismatch")
    if source.runtime.python_version != platform.python_version():
        raise RuntimeError("RD sandbox Python version mismatch")
    runtime = RDSandboxRuntimeIdentity(
        **source.runtime.model_dump(mode="python"),
        runtime_hash=calculated,
        image_digest=_required("STONKS_RD_IMAGE_DIGEST"),
        deterministic=True,
    )
    return RuntimeSettings(
        max_request_bytes=source.max_request_bytes,
        runtime=runtime,
        sandbox=source.sandbox,
    )


def process_request(
    body: bytes,
    settings: RuntimeSettings,
    *,
    runner: CandidateProcessRunner | None = None,
    clock: Callable[[], datetime] | None = None,
    platform_name: Callable[[], str] | None = None,
) -> dict[str, object]:
    if not body or len(body) > settings.max_request_bytes:
        return _envelope(
            413, error={"code": "request_too_large", "message": "Request is too large"}
        )
    try:
        invocation = RDSandboxInvocation.model_validate_json(body)
    except (ValidationError, ValueError):
        return _envelope(
            400, error={"code": "invalid_request", "message": "Request is invalid"}
        )
    root = Path(__file__).resolve().parent
    worker = RDAgentSandboxWorker(
        policy=SandboxWorkerPolicy(
            runtime=settings.runtime,
            sandbox=settings.sandbox,
        ),
        runner=runner
        or PythonCandidateRunner(candidate_runner_path=root / "candidate_runner.py"),
        clock=clock,
        platform_name=platform_name,
    )
    outcome = worker.run(
        invocation.job,
        sandbox_instance_id=invocation.sandbox_instance_id,
    )
    if isinstance(outcome, WorkerFailure):
        return _envelope(
            _status_for(outcome.error.code),
            error=outcome.error.model_dump(mode="json"),
        )
    return _envelope(200, data=outcome.value.model_dump(mode="json"))


def main() -> int:
    root = Path(__file__).resolve().parent
    try:
        settings = load_settings(root)
        body = sys.stdin.buffer.read(settings.max_request_bytes + 1)
        payload = process_request(body, settings)
    except RuntimeError:
        payload = _envelope(
            500,
            error={
                "code": "configuration_invalid",
                "message": "Sandbox configuration is invalid",
            },
        )
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        + "\n"
    )
    return 0


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required sandbox setting is missing: {name}")
    return value


def _status_for(code: str) -> int:
    if code == "deadline_expired":
        return 408
    if code in {"dataset_too_large", "candidate_output_too_large"}:
        return 413
    if code in {
        "runtime_mismatch",
        "sandbox_policy_mismatch",
        "candidate_rejected",
        "candidate_output_invalid",
    }:
        return 409
    return 503


def _envelope(
    status: int,
    *,
    data: object | None = None,
    error: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "success": error is None and status < 400,
        "status": status,
        "data": data,
        "error": error,
        "metadata": None,
    }


if __name__ == "__main__":
    raise SystemExit(main())
