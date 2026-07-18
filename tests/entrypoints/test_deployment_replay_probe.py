from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest

from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError, Success
from stonks_agent.domain.workflow import (
    CreateWorkflowRun,
    WorkflowRunRecord,
    WorkflowStatus,
)
from stonks_contracts.common import stable_payload_hash

ROOT = Path(__file__).resolve().parents[2]


def _load_probe() -> ModuleType:
    spec = spec_from_file_location(
        "deployment_replay_probe",
        ROOT / "scripts" / "deployment_replay_probe.py",
    )
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROBE = _load_probe()


@dataclass
class _State:
    record: WorkflowRunRecord | None = None


class _WorkflowStore:
    def __init__(self, state: _State) -> None:
        self._state = state

    def create(self, request: CreateWorkflowRun) -> object:
        if self._state.record is None:
            self._state.record = WorkflowRunRecord(
                **request.model_dump(),
                status=WorkflowStatus.PENDING,
                version=1,
                updated_at=request.created_at,
            )
        return Success(self._state.record)

    def get(self, run_id: UUID) -> object:
        record = self._state.record
        if record is None or record.run_id != run_id:
            return Failure(
                StructuredError(
                    code=ErrorCode.NOT_FOUND,
                    message="Run was not found",
                )
            )
        return Success(record)

    def transition(
        self,
        run_id: UUID,
        *,
        expected_version: int,
        new_status: WorkflowStatus,
        updated_at: datetime,
    ) -> object:
        record = self._state.record
        if (
            record is None
            or record.run_id != run_id
            or record.version != expected_version
        ):
            return Failure(
                StructuredError(
                    code=ErrorCode.CONFLICT,
                    message="Run version changed concurrently",
                )
            )
        self._state.record = record.model_copy(
            update={
                "status": new_status,
                "version": expected_version + 1,
                "updated_at": updated_at,
            }
        )
        return Success(self._state.record)


class _UnitOfWork:
    def __init__(self, state: _State) -> None:
        self.workflows = _WorkflowStore(state)
        self.commits = 0

    def __enter__(self) -> _UnitOfWork:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1


@contextmanager
def _factory(state: _State) -> Iterator[_UnitOfWork]:
    yield _UnitOfWork(state)


def _run(stage: str, state: _State) -> str:
    return PROBE.execute_stage(stage, lambda: _factory(state))


def test_write_is_deterministic_and_idempotent() -> None:
    state = _State()

    first = _run("write", state)
    second = _run("write", state)

    assert first == second
    payload = json.loads(first)
    assert payload["stage"] == "write"
    assert payload["status"] == "running"
    assert payload["version"] == 2
    assert payload["fresh_inference"] is False
    assert payload["replay_source"] == "persisted_workflow_record"
    assert len(payload["artifact_hash"]) == 64
    output_hash = payload.pop("output_hash")
    assert output_hash == stable_payload_hash(payload)
    assert "password" not in first.casefold()


def test_replay_and_verify_are_restart_safe_and_idempotent() -> None:
    state = _State()
    _run("write", state)

    first_replay = _run("replay", state)
    second_replay = _run("replay", state)
    first_verify = _run("verify", state)
    second_verify = _run("verify", state)

    assert first_replay == second_replay
    assert first_verify == second_verify
    replay = json.loads(first_replay)
    verified = json.loads(first_verify)
    assert replay["status"] == verified["status"] == "succeeded"
    assert replay["version"] == verified["version"] == 3
    assert replay["artifact_hash"] == verified["artifact_hash"]


@pytest.mark.parametrize("stage", ["replay", "verify", "invalid"])
def test_out_of_order_or_unknown_stage_fails_closed(stage: str) -> None:
    with pytest.raises(PROBE.DeploymentReplayProbeError):
        _run(stage, _State())


def test_write_after_replay_fails_closed() -> None:
    state = _State()
    _run("write", state)
    _run("replay", state)

    with pytest.raises(PROBE.DeploymentReplayProbeError):
        _run("write", state)


def test_immutable_record_drift_fails_closed() -> None:
    request = PROBE.replay_request()
    state = _State(
        WorkflowRunRecord(
            **request.model_dump(exclude={"policy_id"}),
            policy_id="drifted-policy",
            status=WorkflowStatus.RUNNING,
            version=2,
            updated_at=request.created_at,
        )
    )

    with pytest.raises(PROBE.DeploymentReplayProbeError):
        _run("write", state)


def test_main_uses_structured_settings_and_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    disposed: list[bool] = []

    class _Engine:
        def dispose(self) -> None:
            disposed.append(True)

    monkeypatch.setattr(PROBE, "load_deployment_settings", lambda env: "settings")
    monkeypatch.setattr(PROBE, "create_runtime_engine", lambda settings: _Engine())
    monkeypatch.setattr(
        PROBE,
        "execute_stage_with_engine",
        lambda stage, engine: '{"success":true}',
    )

    assert PROBE.main(["verify"], {"STONKS_DB_PASSWORD_FILE": "/run/secrets/db"}) == 0
    assert capsys.readouterr().out == '{"success":true}\n'
    assert disposed == [True]


def test_main_returns_only_safe_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        PROBE,
        "load_deployment_settings",
        lambda env: (_ for _ in ()).throw(RuntimeError("password=hunter2")),
    )

    assert PROBE.main(["write"], {}) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": {
            "code": "deployment_replay_failed",
            "message": "Deployment replay probe failed",
        },
        "success": False,
    }
    assert "hunter2" not in captured.err
