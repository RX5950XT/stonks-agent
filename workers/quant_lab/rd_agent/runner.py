"""Fixed-argv subprocess runner with OS resource limits."""

from __future__ import annotations

import importlib
import json
import os
import platform
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol, cast

from pydantic import ValidationError

from stonks_contracts.candidate_scan import scan_candidate_source
from stonks_contracts.rd_agent import (
    CandidateSandboxPolicy,
    RDSandboxDatasetRow,
    SandboxPrediction,
)
from workers.quant_lab.rd_agent.adapter import (
    CandidateProcessError,
    CandidateRunOutput,
)


class _ResourceModule(Protocol):
    RLIMIT_AS: int
    RLIMIT_CORE: int
    RLIMIT_FSIZE: int
    RLIMIT_NOFILE: int
    RLIMIT_NPROC: int
    RLIMIT_CPU: int

    def setrlimit(self, resource: int, limits: tuple[int, int]) -> None: ...


class PythonCandidateRunner:
    def __init__(
        self,
        *,
        candidate_runner_path: Path,
        python_executable: str = sys.executable,
        platform_name: Callable[[], str] | None = None,
    ) -> None:
        self._candidate_runner_path = candidate_runner_path.resolve(strict=True)
        self._python_executable = python_executable
        self._platform_name = platform_name or platform.system

    def run(
        self,
        *,
        source: str,
        rows: tuple[RDSandboxDatasetRow, ...],
        policy: CandidateSandboxPolicy,
    ) -> CandidateRunOutput:
        if self._platform_name() != "Linux":
            raise CandidateProcessError("candidate_process_failed")
        scan_candidate_source(source, policy)
        with TemporaryDirectory(prefix="stonks-rd-candidate-") as temporary:
            root = Path(temporary)
            source_path, input_path, output_path = self._prepare(
                root, source, rows, policy
            )
            command = (
                self._python_executable,
                "-I",
                "-S",
                str(self._candidate_runner_path),
                str(source_path),
                str(input_path),
                str(output_path),
            )
            completed = _execute(command, root, policy)
            _validate_exit(completed.returncode)
            return _read_output(output_path, rows, policy)

    @staticmethod
    def _prepare(
        root: Path,
        source: str,
        rows: tuple[RDSandboxDatasetRow, ...],
        policy: CandidateSandboxPolicy,
    ) -> tuple[Path, Path, Path]:
        source_path = root / "candidate.py"
        input_path = root / "input.json"
        output_path = root / "output.json"
        source_path.write_text(source, encoding="utf-8", newline="\n")
        payload = {
            "allowed_calls": list(policy.allowed_calls),
            "rows": [_candidate_row(value) for value in rows],
        }
        input_path.write_text(
            json.dumps(
                payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ),
            encoding="utf-8",
            newline="\n",
        )
        source_path.chmod(0o444)
        input_path.chmod(0o444)
        return source_path, input_path, output_path


def _candidate_row(value: RDSandboxDatasetRow) -> dict[str, object]:
    return {
        "observation_id": str(value.observation_id),
        "instrument_id": str(value.instrument_id),
        "event_at": value.event_at.isoformat().replace("+00:00", "Z"),
        "feature_available_at": value.feature_available_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "prediction_at": value.prediction_at.isoformat().replace("+00:00", "Z"),
        "features": [str(item) for item in value.features],
    }


def _execute(
    command: tuple[str, ...],
    cwd: Path,
    policy: CandidateSandboxPolicy,
) -> subprocess.CompletedProcess[bytes]:
    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": str(policy.python_hash_seed),
        "PYTHONIOENCODING": "utf-8",
        "TZ": "UTC",
    }
    try:
        if os.name == "posix":
            return subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=policy.timeout_seconds,
                preexec_fn=_resource_limiter(policy),
                start_new_session=True,
            )
        return subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=policy.timeout_seconds,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as error:
        raise CandidateProcessError("candidate_timeout") from error
    except OSError as error:
        raise CandidateProcessError("candidate_process_failed") from error


def _resource_limiter(policy: CandidateSandboxPolicy) -> Callable[[], None]:
    def apply() -> None:
        resource = cast(_ResourceModule, importlib.import_module("resource"))

        memory = policy.memory_megabytes * 1024 * 1024
        cpu = max(1, policy.timeout_seconds)
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (policy.max_output_bytes, policy.max_output_bytes),
        )
        resource.setrlimit(resource.RLIMIT_NOFILE, (policy.max_open_files,) * 2)
        resource.setrlimit(resource.RLIMIT_NPROC, (policy.max_processes,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))

    return apply


def _validate_exit(returncode: int) -> None:
    if returncode == 0:
        return
    if returncode == 70:
        raise CandidateProcessError("candidate_memory_exceeded")
    cpu_signals = {
        int(getattr(signal, "SIGKILL", 9)),
        int(getattr(signal, "SIGXCPU", 24)),
    }
    if returncode < 0 and -returncode in cpu_signals:
        raise CandidateProcessError("candidate_timeout")
    raise CandidateProcessError("candidate_process_failed")


def _read_output(
    path: Path,
    rows: tuple[RDSandboxDatasetRow, ...],
    policy: CandidateSandboxPolicy,
) -> CandidateRunOutput:
    try:
        if not path.is_file() or path.stat().st_size > policy.max_output_bytes:
            raise CandidateProcessError("candidate_output_too_large")
        payload = json.loads(path.read_bytes())
        if not isinstance(payload, list):
            raise ValueError
        predictions = tuple(
            SandboxPrediction.model_validate(value) for value in payload
        )
        expected_ids = tuple(value.observation_id for value in rows)
        if tuple(value.observation_id for value in predictions) != expected_ids:
            raise ValueError
        return CandidateRunOutput(predictions=predictions)
    except CandidateProcessError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
    ) as error:
        raise CandidateProcessError("candidate_process_failed") from error
