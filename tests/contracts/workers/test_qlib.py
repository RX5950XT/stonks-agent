from __future__ import annotations

import sys
import tomllib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
import yaml
from fastapi.testclient import TestClient

from stonks_contracts.quant_lab import (
    QuantCostModelSpec,
    QuantDatasetArtifact,
    QuantDatasetRow,
    QuantFeatureSpec,
    QuantLabelSpec,
    QuantModelSpec,
    QuantPrediction,
    QuantResearchJob,
    QuantRuntimeIdentity,
    QuantSplitSpec,
    QuantUniverseSpec,
)
from stonks_service_auth import ServiceReceiver

ROOT = Path(__file__).resolve().parents[3]
WORKER_ROOT = ROOT / "workers" / "quant_lab"
sys.path.insert(0, str(ROOT))

from fixtures.service_auth import (  # noqa: E402
    ExactServiceAuthenticator,
    authorization_headers,
)

from workers.quant_lab.app import create_app  # noqa: E402
from workers.quant_lab.qlib_adapter import (  # noqa: E402
    QuantLabWorker,
    RuntimeOutput,
    WorkerFailure,
    WorkerPolicy,
    compute_runtime_hash,
)

NOW = datetime(2026, 7, 13, 6, tzinfo=UTC)
START = datetime(2025, 1, 2, 21, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
INSTRUMENTS = (
    UUID("20000000-0000-4000-8000-000000000921"),
    UUID("20000000-0000-4000-8000-000000000922"),
)


def _runtime() -> QuantRuntimeIdentity:
    return QuantRuntimeIdentity(
        worker_version="quant-lab-worker/0.1.0",
        qlib_commit="d5379c520f66a39953bad76234a7019a72796fd0",
        qlib_source_hash=HASH_A,
        qlib_version="0.9.8.dev0+d5379c52",
        runtime_hash=HASH_B,
        python_version="3.12.9",
        numpy_version="2.2.6",
        pandas_version="2.2.3",
        sklearn_version="1.7.2",
    )


def _dataset() -> QuantDatasetArtifact:
    feature_spec = QuantFeatureSpec(
        names=("return_1", "return_5", "volatility_5", "volume_change_1"),
        lookback_bars=6,
    )
    label_spec = QuantLabelSpec(name="forward_return", horizon_bars=1)
    universe_spec = QuantUniverseSpec(
        instrument_ids=INSTRUMENTS,
        historical_membership_artifact_ref=f"sha256:{HASH_A}",
    )
    rows = tuple(
        QuantDatasetRow(
            row_id=uuid5(NAMESPACE_URL, f"qlib-worker-row:{index}"),
            instrument_id=INSTRUMENTS[index % 2],
            event_at=START + timedelta(days=index // 2, seconds=index % 2),
            feature_available_at=START + timedelta(days=index // 2, seconds=index % 2),
            label_outcome_at=START
            + timedelta(days=index // 2, hours=1, seconds=index % 2),
            label_available_at=START
            + timedelta(
                days=index // 2,
                hours=1,
                minutes=1,
                seconds=index % 2,
            ),
            historical_universe_known_at=START - timedelta(days=30),
            in_historical_universe=True,
            features=(
                Decimal(index + 1) / Decimal(100),
                Decimal(index + 2) / Decimal(100),
                Decimal("0.01"),
                Decimal("0.02"),
            ),
            label=Decimal("0.01") if index % 2 else Decimal("-0.01"),
        )
        for index in range(18)
    )
    return QuantDatasetArtifact(
        dataset_snapshot_id=UUID("20000000-0000-4000-8000-000000000923"),
        source_snapshot_artifact_ref=f"sha256:{HASH_A}",
        source_data_hash=HASH_B,
        as_of=NOW,
        feature_spec=feature_spec,
        label_spec=label_spec,
        universe_spec=universe_spec,
        rows=rows,
    )


def _job(**changes: object) -> QuantResearchJob:
    dataset = _dataset()
    payload: dict[str, object] = {
        "request_id": UUID("20000000-0000-4000-8000-000000000924"),
        "run_id": UUID("20000000-0000-4000-8000-000000000925"),
        "job_id": UUID("20000000-0000-4000-8000-000000000926"),
        "attempt_generation": 3,
        "attempt_nonce": "nonce-3",
        "dataset_artifact_ref": f"sha256:{dataset.payload_hash()}",
        "dataset": dataset,
        "feature_spec": dataset.feature_spec,
        "label_spec": dataset.label_spec,
        "universe_spec": dataset.universe_spec,
        "cost_model": QuantCostModelSpec(fee_bps="1", slippage_bps="5"),
        "split_policy": QuantSplitSpec(
            train_start=START,
            train_end=START + timedelta(days=4),
            valid_start=START + timedelta(days=5),
            valid_end=START + timedelta(days=6),
            test_start=START + timedelta(days=7),
            test_end=START + timedelta(days=8, seconds=1),
            purge_observations=1,
            embargo_observations=1,
        ),
        "model_spec": QuantModelSpec(
            algorithm="qlib_linear_ols",
            fit_intercept=False,
            deterministic=True,
        ),
        "runtime": _runtime(),
        "requested_at": NOW,
        "deadline": NOW + timedelta(minutes=5),
    }
    return QuantResearchJob.model_validate(payload | changes)


class _FakeRuntime:
    def __init__(self, *, identity: QuantRuntimeIdentity | None = None) -> None:
        self.identity = identity or _runtime()
        self.calls = 0

    def fit_predict(self, job: QuantResearchJob) -> RuntimeOutput:
        self.calls += 1
        rows = tuple(
            value
            for value in job.dataset.rows
            if job.split_policy.test_start
            <= value.event_at
            <= job.split_policy.test_end
        )
        return RuntimeOutput(
            predictions=tuple(
                QuantPrediction(
                    row_id=value.row_id,
                    instrument_id=value.instrument_id,
                    event_at=value.event_at,
                    predicted_return=(
                        Decimal("0.005")
                        if value.instrument_id == INSTRUMENTS[1]
                        else Decimal("-0.005")
                    ),
                    actual_return=value.label,
                )
                for value in rows
            ),
            model_parameters=(
                Decimal("0.1"),
                Decimal("0.2"),
                Decimal("0.3"),
                Decimal("0.4"),
            ),
            warnings=(),
        )


def _worker(runtime: _FakeRuntime | None = None) -> QuantLabWorker:
    selected = runtime or _FakeRuntime()
    return QuantLabWorker(
        policy=WorkerPolicy(runtime=_runtime(), max_rows=10_000),
        runtime=selected,
        clock=lambda: NOW + timedelta(minutes=1),
    )


def test_worker_replays_same_job_to_same_artifact_hashes() -> None:
    runtime = _FakeRuntime()
    worker = _worker(runtime)

    first = worker.research(_job())
    second = worker.research(_job())

    assert not isinstance(first, WorkerFailure)
    assert first == second
    assert runtime.calls == 2
    result = first.value.result
    assert result.deterministic is True
    assert len(result.predictions) == 4
    assert (
        result.prediction_artifact_hash == second.value.result.prediction_artifact_hash
    )
    assert result.model_artifact_hash == second.value.result.model_artifact_hash
    assert all(value.research_only for value in result.positions)


@pytest.mark.parametrize(
    "failure",
    ["runtime", "deadline", "post_deadline", "alignment", "split", "binding"],
)
def test_worker_fails_closed_before_returning_invalid_result(failure: str) -> None:
    job = _job()
    runtime = _FakeRuntime()

    def before_deadline() -> datetime:
        return NOW + timedelta(minutes=1)

    clock = before_deadline
    if failure == "runtime":
        runtime.identity = _runtime().model_copy(update={"runtime_hash": HASH_A})
    if failure == "deadline":

        def initial_deadline_expired() -> datetime:
            return NOW + timedelta(minutes=6)

        clock = initial_deadline_expired
    if failure == "post_deadline":
        times = iter((NOW + timedelta(minutes=1), NOW + timedelta(minutes=6)))

        def deadline_expires_during_runtime() -> datetime:
            return next(times)

        clock = deadline_expires_during_runtime
    if failure == "alignment":

        class _BadRuntime(_FakeRuntime):
            def fit_predict(self, job: QuantResearchJob) -> RuntimeOutput:
                output = super().fit_predict(job)
                return output.model_copy(
                    update={"predictions": output.predictions[:-1]}
                )

        runtime = _BadRuntime()
    if failure == "split":
        split = job.split_policy.model_copy(
            update={"test_start": START + timedelta(days=20)}
        )
        job = job.model_copy(update={"split_policy": split})
    if failure == "binding":
        job = job.model_copy(update={"dataset_artifact_ref": f"sha256:{HASH_A}"})
    worker = QuantLabWorker(
        policy=WorkerPolicy(runtime=_runtime(), max_rows=10_000),
        runtime=runtime,
        clock=clock,
    )

    result = worker.research(job)

    assert isinstance(result, WorkerFailure)


def test_worker_converts_runtime_exception_to_generic_structured_failure() -> None:
    class _UnavailableRuntime(_FakeRuntime):
        def fit_predict(self, job: QuantResearchJob) -> RuntimeOutput:
            raise ImportError("sensitive runtime path")

    result = _worker(_UnavailableRuntime()).research(_job())

    assert isinstance(result, WorkerFailure)
    assert result.error.code == "research_failed"
    assert "sensitive" not in result.error.message


def test_http_surface_is_bounded_closed_and_has_unified_envelope() -> None:
    job = _job()
    client = TestClient(
        create_app(
            worker=_worker(),
            authenticator=ExactServiceAuthenticator.for_request(
                job,
                receiver=ServiceReceiver.QUANT_LAB,
            ),
            max_request_bytes=1_000_000,
        )
    )

    health = client.get("/healthz")
    success = client.post(
        "/v1/research",
        content=job.canonical_json(),
        headers={**authorization_headers(), "content-type": "application/json"},
    )
    encoded = client.post(
        "/v1/research",
        content=job.canonical_json(),
        headers={
            **authorization_headers(),
            "content-type": "application/json",
            "content-encoding": "gzip",
        },
    )
    hostile_lengths = tuple(
        client.post(
            "/v1/research",
            content=b"{}",
            headers={
                **authorization_headers(),
                "content-length": declared,
                "content-type": "application/json",
            },
        )
        for declared in ("9" * 5_000, "not-a-number")
    )
    invalid = client.post(
        "/v1/research",
        content=b'{"target_weight":"1"}',
        headers={**authorization_headers(), "content-type": "application/json"},
    )
    denied = client.post(
        "/v1/research",
        content=job.canonical_json(),
        headers={
            "authorization": "Bearer wrong-but-long-service-token",
            "content-type": "application/json",
        },
    )
    wrong_target = TestClient(
        create_app(
            worker=_worker(),
            authenticator=ExactServiceAuthenticator.for_request(
                job,
                receiver=ServiceReceiver.QUANT_LAB,
                target_identifier=UUID(int=999),
            ),
        )
    ).post(
        "/v1/research",
        content=job.canonical_json(),
        headers={**authorization_headers(), "content-type": "application/json"},
    )

    assert health.json()["data"]["qlib_commit"] == _runtime().qlib_commit
    assert success.status_code == 200
    assert success.json()["data"]["attempt_nonce"] == "nonce-3"
    assert encoded.status_code == 415
    assert all(response.status_code == 413 for response in hostile_lengths)
    assert all(
        response.json()["error"]["code"] == "request_too_large"
        for response in hostile_lengths
    )
    assert invalid.status_code == 400
    assert denied.status_code == 401
    assert wrong_target.status_code == 403
    assert set(success.json()) == {"success", "status", "data", "error", "metadata"}


def test_worker_dependency_and_container_policy_stay_out_of_core() -> None:
    project = tomllib.loads((WORKER_ROOT / "pyproject.toml").read_text("utf-8"))
    worker_dependencies = tuple(project["project"]["dependencies"])
    core_project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    core_dependencies = tuple(core_project["project"]["dependencies"])
    dockerfile = (WORKER_ROOT / "Dockerfile").read_text("utf-8")
    compose = yaml.safe_load(
        (ROOT / "infra" / "compose.quant-lab.yaml").read_text("utf-8")
    )
    service = compose["services"]["quant-lab"]

    assert not any(
        "pyqlib" in value or value.startswith("qlib") for value in core_dependencies
    )
    assert not any("numpy" in value or "pandas" in value for value in core_dependencies)
    assert any("numpy" in value for value in worker_dependencies)
    assert any("pandas" in value for value in worker_dependencies)
    assert "d5379c520f66a39953bad76234a7019a72796fd0" in dockerfile
    assert "ADD --checksum=sha256:" in dockerfile
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["networks"] == ["quant-lab-internal"]
    assert compose["networks"]["quant-lab-internal"]["internal"] is True
    forbidden = ("DB", "DATABASE", "BROKER", "QUEUE", "EXECUTION", "PROVIDER", "TOKEN")
    assert not any(
        marker in key.upper()
        for key in service.get("environment", {})
        for marker in forbidden
    )


def test_quant_lab_lock_and_notice_are_pinned() -> None:
    lock = (WORKER_ROOT / "uv.lock").read_text("utf-8")
    notice = (WORKER_ROOT / "NOTICE.md").read_text("utf-8")

    assert "version = 1" in lock
    assert "d5379c520f66a39953bad76234a7019a72796fd0" in notice
    assert "MIT License" in notice
    assert "Microsoft Corporation" in notice


def test_runtime_configuration_binds_source_versions_and_worker_files() -> None:
    configuration = yaml.safe_load(
        (ROOT / "config" / "workers" / "quant_lab.yaml").read_text("utf-8")
    )
    runtime = QuantRuntimeIdentity.model_validate(configuration["runtime"])

    assert runtime.qlib_source_hash == (
        "3aaefc2f1711376ef6e603ffcf953e6f377eed90d6367fe2eb0cbcd4cfcb2276"
    )
    assert runtime.python_version == "3.12.12"
    assert runtime.runtime_hash == compute_runtime_hash(WORKER_ROOT)
