from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from stonks_service_auth import ServiceReceiver, ServiceResourceKind

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fixtures.service_auth import (  # noqa: E402
    ExactServiceAuthenticator,
    authorization_headers,
)

from sidecars.lean.adapter import (  # noqa: E402
    AdapterPolicy,
    EngineFailure,
    EngineFillTrace,
    EngineOrderTrace,
    EngineRunTrace,
    LeanAdapter,
    WorkerFailure,
    WorkerSuccess,
)
from sidecars.lean.app import create_app  # noqa: E402
from stonks_contracts.backtest import (  # noqa: E402
    BacktestEngineKind,
    BacktestOrderStatus,
)
from tests.contracts.backtest.test_backtest_contract import (  # noqa: E402
    BAR_2_ID,
    DAY_2_OPEN,
    ORDER_ID,
    REQUESTED,
    canonical_result,
    job,
)


@dataclass
class FakeBackend:
    response: EngineRunTrace | EngineFailure
    calls: int = 0

    def run(self, request: object) -> EngineRunTrace | EngineFailure:
        self.calls += 1
        return self.response


def trace() -> EngineRunTrace:
    return EngineRunTrace(
        orders=(
            EngineOrderTrace(
                order_id=ORDER_ID,
                status=BacktestOrderStatus.FILLED,
                reason=None,
            ),
        ),
        fills=(
            EngineFillTrace(
                external_fill_id="T-001",
                order_id=ORDER_ID,
                quantity=Decimal("10"),
                raw_price=Decimal("100"),
                raw_fees=Decimal("0"),
                raw_event_hash="c" * 64,
                occurred_at=DAY_2_OPEN,
                source_bar_id=BAR_2_ID,
            ),
        ),
    )


def adapter(response: EngineRunTrace | EngineFailure) -> LeanAdapter:
    request = job(BacktestEngineKind.LEAN)
    return LeanAdapter(
        policy=AdapterPolicy(
            runtime=request.runtime,
            max_orders=100,
            max_bars=10_000,
        ),
        backend=FakeBackend(response),
        clock=lambda: REQUESTED,
    )


def test_adapter_maps_complete_trace_to_canonical_replay() -> None:
    request = job(BacktestEngineKind.LEAN)
    worker = adapter(trace())

    first = worker.run(request)
    second = worker.run(request)

    assert isinstance(first, WorkerSuccess)
    assert isinstance(second, WorkerSuccess)
    first.value.validate_against(request)
    assert first.value == second.value
    assert first.value.semantic_hash == canonical_result(request).semantic_hash
    assert first.value.fills[0].external_ref == ("lean:T-001:raw-sha256:" + "c" * 64)


def test_runtime_drift_duplicate_trace_and_backend_failure_fail_closed() -> None:
    request = job(BacktestEngineKind.LEAN)
    wrong_runtime = request.model_copy(
        update={
            "runtime": request.runtime.model_copy(update={"runtime_hash": "d" * 64})
        }
    )
    duplicate = EngineRunTrace(
        orders=trace().orders,
        fills=(trace().fills[0], trace().fills[0]),
    )

    drift = adapter(trace()).run(wrong_runtime)
    invalid = adapter(duplicate).run(request)
    unavailable = adapter(
        EngineFailure(code="engine_unavailable", message="safe failure")
    ).run(request)

    assert isinstance(drift, WorkerFailure)
    assert drift.error.code == "runtime_mismatch"
    assert isinstance(invalid, WorkerFailure)
    assert invalid.error.code == "invalid_engine_output"
    assert isinstance(unavailable, WorkerFailure)
    assert unavailable.error.code == "engine_unavailable"
    assert unavailable.error.message == "safe failure"


def test_non_finite_engine_numbers_fail_closed() -> None:
    request = job(BacktestEngineKind.LEAN)
    for field, value in (
        ("quantity", Decimal("NaN")),
        ("raw_price", Decimal("Infinity")),
        ("raw_fees", Decimal("-Infinity")),
    ):
        bad_fill = replace(trace().fills[0], **{field: value})

        result = adapter(EngineRunTrace(orders=trace().orders, fills=(bad_fill,))).run(
            request
        )

        assert isinstance(result, WorkerFailure)
        assert result.error.code == "invalid_engine_output"


def test_order_bar_work_limit_rejects_before_backend_call() -> None:
    request = job(BacktestEngineKind.LEAN)
    backend = FakeBackend(trace())
    worker = LeanAdapter(
        policy=AdapterPolicy(
            runtime=request.runtime,
            max_orders=100,
            max_bars=100,
            max_order_bar_evaluations=1,
        ),
        backend=backend,
        clock=lambda: REQUESTED,
    )

    result = worker.run(request)

    assert isinstance(result, WorkerFailure)
    assert result.error.code == "job_too_large"
    assert backend.calls == 0


def test_completion_time_changes_artifact_id_not_semantics() -> None:
    request = job(BacktestEngineKind.LEAN)
    first_worker = adapter(trace())
    second_worker = adapter(trace())
    second_worker = replace(
        second_worker, clock=lambda: REQUESTED + timedelta(seconds=1)
    )

    first = first_worker.run(request)
    second = second_worker.run(request)

    assert isinstance(first, WorkerSuccess)
    assert isinstance(second, WorkerSuccess)
    assert first.value.result_id != second.value.result_id
    assert first.value.semantic_hash == second.value.semantic_hash


def test_http_surface_is_bounded_typed_and_has_no_execution_route() -> None:
    request = job(BacktestEngineKind.LEAN)
    worker = adapter(trace())
    client = TestClient(
        create_app(
            adapter=worker,
            authenticator=ExactServiceAuthenticator.for_request(
                request,
                receiver=ServiceReceiver.LEAN,
                kind=ServiceResourceKind.BACKTEST_JOB,
            ),
            max_request_bytes=2_000_000,
        )
    )

    health = client.get("/healthz")
    accepted = client.post(
        "/v1/backtests",
        content=request.model_dump_json(),
        headers={
            **authorization_headers(),
            "content-type": "application/json",
        },
    )
    rejected = client.post(
        "/v1/backtests",
        content=b"{}",
        headers={
            **authorization_headers(),
            "content-type": "text/plain",
        },
    )
    unauthorized = client.post("/v1/backtests", json={})
    hostile_length = client.post(
        "/v1/backtests",
        content=b"{}",
        headers={
            **authorization_headers(),
            "content-length": "9" * 5_000,
            "content-type": "application/json",
        },
    )
    hostile_decimal = request.model_dump(mode="json")
    hostile_decimal["initial_cash"][0]["amount"] = "1e10000"
    invalid_decimal = client.post(
        "/v1/backtests",
        json=hostile_decimal,
        headers=authorization_headers(),
    )
    wrong_target = TestClient(
        create_app(
            adapter=adapter(trace()),
            authenticator=ExactServiceAuthenticator.for_request(
                request,
                receiver=ServiceReceiver.LEAN,
                kind=ServiceResourceKind.BACKTEST_JOB,
                target_identifier="other-job",
            ),
            max_request_bytes=2_000_000,
        )
    ).post(
        "/v1/backtests",
        content=request.model_dump_json(),
        headers={**authorization_headers(), "content-type": "application/json"},
    )

    assert health.status_code == 200
    assert health.json()["data"]["engine"] == "lean"
    assert accepted.status_code == 200
    assert accepted.json()["data"]["execution_mode"] == "backtest"
    assert rejected.status_code == 415
    assert unauthorized.status_code == 401
    assert hostile_length.status_code == 413
    assert invalid_decimal.status_code == 400
    assert wrong_target.status_code == 403
    assert isinstance(worker.backend, FakeBackend)
    assert worker.backend.calls == 1
    assert client.post("/v1/orders", json={}).status_code == 404
