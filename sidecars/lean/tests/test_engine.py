from __future__ import annotations

import json
import sys
import zipfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from sidecars.lean.adapter import EngineFailure, EngineRunTrace  # noqa: E402
from sidecars.lean.engine import (  # noqa: E402
    LEAN_ENGINE_VERSION,
    LeanEngineBackend,
    LeanProcessRequest,
    _schedule,
    _write_job_inputs,
)
from stonks_contracts.backtest import (  # noqa: E402
    BacktestEngineKind,
    BacktestRuntimeIdentity,
)
from tests.contracts.backtest.test_backtest_contract import (  # noqa: E402
    BAR_2_ID,
    DAY_2_OPEN,
    REQUESTED,
    job,
)


def request():
    base = job(BacktestEngineKind.LEAN)
    runtime = BacktestRuntimeIdentity(
        engine=BacktestEngineKind.LEAN,
        engine_version=LEAN_ENGINE_VERSION,
        adapter_version="0.1.0",
        runtime_hash="c" * 64,
        image_digest="sha256:" + "d" * 64,
        deterministic=True,
    )
    return base.model_copy(update={"runtime": runtime})


@dataclass
class RecordingRunner:
    exit_code: int = 0
    malformed: bool = False
    seen: LeanProcessRequest | None = None

    def run(self, process: LeanProcessRequest) -> int:
        self.seen = process
        if self.exit_code == 0:
            payload: object = (
                {"not": "trace"}
                if self.malformed
                else {
                    "engine_version": LEAN_ENGINE_VERSION,
                    "events": [
                        {
                            "child_id": process.child_ids[0],
                            "lean_order_id": 1,
                            "order_event_id": 2,
                            "status": "Filled",
                            "direction": "Buy",
                            "fill_quantity": "10",
                            "fill_price": "100",
                            "order_fee": "0",
                            "utc_time": "2026-07-14T14:31:00Z",
                        }
                    ],
                }
            )
            process.trace_path.write_text(
                json.dumps(payload, sort_keys=True), encoding="utf-8"
            )
        return self.exit_code


def test_schedule_and_generated_lean_data_are_deterministic(tmp_path: Path) -> None:
    target = request()
    schedule = _schedule(target, max_children=10)

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _write_job_inputs(first, target, schedule, base_data=None)
    _write_job_inputs(second, target, schedule, base_data=None)

    assert len(schedule) == 1
    assert schedule[0].source_bar_id == BAR_2_ID
    assert schedule[0].source_opens_at == DAY_2_OPEN
    first_schedule = (first / "schedule.json").read_bytes()
    assert first_schedule == (second / "schedule.json").read_bytes()
    archive = (
        first / "data" / "equity" / "usa" / "minute" / "aapl" / ("20260714_trade.zip")
    )
    with zipfile.ZipFile(archive) as source:
        rows = (
            source.read("20260714_aapl_minute_trade.csv").decode("ascii").splitlines()
        )
    assert rows == ["37800000,1000000,1010000,990000,1000000,1000"]
    assert (first / "data" / "equity" / "usa" / "map_files" / "aapl.csv").is_file()
    assert (first / "data" / "equity" / "usa" / "factor_files" / "aapl.csv").is_file()
    assert (first / "data" / "symbol-properties" / "security-database.csv").is_file()
    assert not (first / "data" / "market-hours" / "security-database.csv").exists()


def test_backend_runs_fixed_command_with_sanitized_environment(tmp_path: Path) -> None:
    runner = RecordingRunner()
    launcher = tmp_path / "QuantConnect.Lean.Launcher.dll"
    algorithm = tmp_path / "Stonks.Lean.Algorithm.dll"
    launcher.write_bytes(b"launcher")
    algorithm.write_bytes(b"algorithm")
    backend = LeanEngineBackend(
        launcher=launcher,
        algorithm=algorithm,
        template=ROOT / "sidecars" / "lean" / "appsettings.template.json",
        base_data=None,
        workspace_root=tmp_path / "jobs",
        process_runner=runner,
        clock=lambda: REQUESTED,
        max_engine_seconds=30,
    )

    outcome = backend.run(request())

    assert isinstance(outcome, EngineRunTrace)
    assert len(outcome.fills) == 1
    assert outcome.fills[0].source_bar_id == BAR_2_ID
    assert runner.seen is not None
    assert runner.seen.command[:2] == ("dotnet", str(launcher.resolve()))
    assert runner.seen.command[2] == "--config"
    assert runner.seen.timeout_seconds == 30
    assert runner.seen.env == {
        "COMPlus_EnableDiagnostics": "0",
        "DOTNET_EnableDiagnostics": "0",
        "DOTNET_ROOT": "/usr/share/dotnet",
        "HOME": "/tmp/sidecar",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "TZ": "UTC",
    }


@pytest.mark.parametrize(
    ("runner", "expected"),
    [
        (RecordingRunner(exit_code=9), "engine_failed"),
        (RecordingRunner(malformed=True), "engine_invalid_output"),
    ],
)
def test_process_and_trace_failures_are_structured(
    tmp_path: Path,
    runner: RecordingRunner,
    expected: str,
) -> None:
    launcher = tmp_path / "launcher.dll"
    algorithm = tmp_path / "algorithm.dll"
    launcher.write_bytes(b"x")
    algorithm.write_bytes(b"x")
    backend = LeanEngineBackend(
        launcher=launcher,
        algorithm=algorithm,
        template=ROOT / "sidecars" / "lean" / "appsettings.template.json",
        base_data=None,
        workspace_root=tmp_path / "jobs",
        process_runner=runner,
        clock=lambda: REQUESTED,
    )

    outcome = backend.run(request())

    assert isinstance(outcome, EngineFailure)
    assert outcome.code == expected


def test_runtime_drift_and_expired_deadline_fail_before_process(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backend = LeanEngineBackend(
        launcher=tmp_path / "missing-launcher.dll",
        algorithm=tmp_path / "missing-algorithm.dll",
        template=ROOT / "sidecars" / "lean" / "appsettings.template.json",
        base_data=None,
        workspace_root=tmp_path / "jobs",
        process_runner=runner,
        clock=lambda: REQUESTED + timedelta(minutes=6),
    )
    target = request().model_copy(
        update={
            "runtime": request().runtime.model_copy(update={"engine_version": "drift"})
        }
    )

    drift = backend.run(target)
    expired = backend.run(request())

    assert isinstance(drift, EngineFailure)
    assert drift.code == "runtime_mismatch"
    assert isinstance(expired, EngineFailure)
    assert expired.code == "deadline_expired"
    assert runner.seen is None


def test_schedule_cap_fails_before_allocating_extra_child() -> None:
    with pytest.raises(ValueError, match="schedule exceeds"):
        _schedule(request(), max_children=0)


def test_backend_reports_oversized_native_schedule_separately(tmp_path: Path) -> None:
    target = request()
    second = target.orders[0].model_copy(
        update={
            "order_id": UUID("10000000-0000-4000-8000-000000000099"),
            "sequence": 2,
        }
    )
    target = target.model_copy(update={"orders": (*target.orders, second)})
    runner = RecordingRunner()
    launcher = tmp_path / "launcher.dll"
    algorithm = tmp_path / "algorithm.dll"
    launcher.write_bytes(b"x")
    algorithm.write_bytes(b"x")
    backend = LeanEngineBackend(
        launcher=launcher,
        algorithm=algorithm,
        template=ROOT / "sidecars" / "lean" / "appsettings.template.json",
        base_data=None,
        workspace_root=tmp_path / "jobs",
        process_runner=runner,
        clock=lambda: REQUESTED,
        max_schedule_children=1,
    )

    outcome = backend.run(target)

    assert isinstance(outcome, EngineFailure)
    assert outcome.code == "job_too_large"
    assert runner.seen is None
