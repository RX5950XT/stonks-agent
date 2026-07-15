from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

from workers.quant_lab.rd_agent.adapter import CandidateProcessError
from workers.quant_lab.rd_agent.candidate_runner import (
    freeze_value,
    restricted_builtins,
)
from workers.quant_lab.rd_agent.runner import PythonCandidateRunner

from .test_adapter import SAFE_SOURCE, dataset, sandbox_policy


def test_candidate_values_are_recursively_immutable() -> None:
    value = freeze_value(
        [
            {
                "observation_id": "id-1",
                "features": ["0.1", "0.2"],
            }
        ]
    )

    assert isinstance(value, tuple)
    assert isinstance(value[0], MappingProxyType)
    assert value[0]["features"] == ("0.1", "0.2")
    with pytest.raises(TypeError):
        value[0]["features"] = ()  # type: ignore[index]


def test_candidate_builtins_are_exactly_policy_allowlisted() -> None:
    active = sandbox_policy()

    values = restricted_builtins(active.allowed_calls)

    assert frozenset(values) == frozenset(active.allowed_calls)
    assert {
        "__import__",
        "compile",
        "eval",
        "exec",
        "getattr",
        "open",
    }.isdisjoint(values)


def test_python_runner_executes_safe_candidate_with_fixed_protocol() -> None:
    runner = PythonCandidateRunner(
        candidate_runner_path=Path(
            "workers/quant_lab/rd_agent/candidate_runner.py"
        ).resolve(),
        python_executable=sys.executable,
        platform_name=lambda: "Linux",
    )

    output = runner.run(
        source=SAFE_SOURCE,
        rows=dataset().rows,
        policy=sandbox_policy(),
    )

    assert tuple(item.observation_id for item in output.predictions) == tuple(
        item.observation_id for item in dataset().rows
    )
    assert tuple(str(item.predicted_return) for item in output.predictions) == (
        "0.01",
        "0.02",
        "0.03",
    )


def test_child_runtime_freezes_rows_even_if_static_scan_were_bypassed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate.py"
    payload = tmp_path / "input.json"
    output = tmp_path / "output.json"
    source.write_text(
        "def compute(rows):\n    rows[0]['features'] = ()\n    return []\n",
        encoding="utf-8",
    )
    payload.write_text(
        json.dumps(
            {
                "allowed_calls": list(sandbox_policy().allowed_calls),
                "rows": [
                    {
                        "observation_id": "id-1",
                        "instrument_id": "instrument-1",
                        "event_at": "2026-01-01T00:00:00Z",
                        "feature_available_at": "2026-01-01T00:00:00Z",
                        "prediction_at": "2026-01-01T00:00:00Z",
                        "features": ["0.1"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        (
            sys.executable,
            "-I",
            "-S",
            str(Path("workers/quant_lab/rd_agent/candidate_runner.py").resolve()),
            str(source),
            str(payload),
            str(output),
        ),
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    assert result.returncode != 0
    assert not output.exists()


def test_cpu_bomb_is_bounded_by_wall_timeout() -> None:
    runner = PythonCandidateRunner(
        candidate_runner_path=Path(
            "workers/quant_lab/rd_agent/candidate_runner.py"
        ).resolve(),
        python_executable=sys.executable,
        platform_name=lambda: "Linux",
    )
    source = "def compute(rows):\n    return sum(range(1000000000000))\n"
    active = sandbox_policy().model_copy(update={"timeout_seconds": 1})

    with pytest.raises(CandidateProcessError) as captured:
        runner.run(source=source, rows=dataset().rows, policy=active)

    assert captured.value.code == "candidate_timeout"


def test_runner_rejects_non_linux_platform_before_starting_process() -> None:
    runner = PythonCandidateRunner(
        candidate_runner_path=Path(
            "workers/quant_lab/rd_agent/candidate_runner.py"
        ).resolve(),
        python_executable=sys.executable,
        platform_name=lambda: "Windows",
    )

    with pytest.raises(CandidateProcessError) as captured:
        runner.run(
            source=SAFE_SOURCE,
            rows=dataset().rows,
            policy=sandbox_policy(),
        )

    assert captured.value.code == "candidate_process_failed"
