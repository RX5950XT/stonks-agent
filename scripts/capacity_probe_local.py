"""Deterministic in-process workloads for capacity contract evidence."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from fastapi.testclient import TestClient

from scripts.capacity_probe_common import (
    EXPECTED_SCHEMA_REVISION,
    FIXED_NOW,
    ProbeError,
)
from stonks_agent.adapters.fakes.platform import build_fake_run_service
from stonks_agent.application.workflows.run_cycle import RunCycleRequest
from stonks_agent.domain.errors import Success
from stonks_agent.domain.signal import ForecastOutputArtifact, ForecastRequest
from stonks_agent.entrypoints.api.deployment import (
    DeploymentReadiness,
    create_deployment_app,
)
from stonks_contracts.common import stable_payload_hash
from stonks_contracts.market_data import DataQuality, DataQualityStatus
from stonks_contracts.signal import ForecastSignal


class _ReadyProbe:
    def check(self) -> Success[DeploymentReadiness]:
        return Success(
            DeploymentReadiness(
                database=True,
                schema_current=True,
                execution_mode="paper",
                migration_revision=EXPECTED_SCHEMA_REVISION,
            )
        )


def asgi_security_contract_once(index: int) -> str:
    _require_index(index)
    app = create_deployment_app(_ReadyProbe(), build_revision="capacity-probe")
    with TestClient(app) as client:
        accepted = client.get("/healthz")
        rejected = client.get("/healthz", headers={"X-Forwarded-For": "127.0.0.1"})
    accepted_body = accepted.json()
    rejected_body = rejected.json()
    valid = (
        accepted.status_code == 200
        and accepted_body.get("success") is True
        and accepted_body.get("data", {}).get("execution_mode") == "paper"
        and accepted.headers.get("x-content-type-options") == "nosniff"
        and "default-src 'none'" in accepted.headers.get("content-security-policy", "")
        and rejected.status_code == 400
        and rejected_body.get("error", {}).get("code") == "invalid_input"
    )
    if not valid:
        raise ProbeError("ASGI security contract validation failed")
    return stable_payload_hash(
        {
            "accepted": accepted_body,
            "rejected_status": rejected.status_code,
            "contract": "deployment-health-security/1",
        }
    )


def paper_cycle_once(index: int) -> str:
    _require_index(index)
    service = build_fake_run_service(clock=FIXED_NOW, seed=f"capacity-{index}")
    result = service.run(
        RunCycleRequest(
            idempotency_key=f"capacity-{index}",
            account_id=f"paper-capacity-{index}",
            instrument_id="instrument-aapl",
            symbol="AAPL",
            as_of=FIXED_NOW,
            evidence_available_at=FIXED_NOW,
            signal_value=Decimal("0.80"),
            signal_confidence=Decimal("0.90"),
        )
    )
    journal = result.journal_transaction
    replay = service.replay(result.run_id)
    if (
        result.status != "completed"
        or journal is None
        or not journal.is_balanced()
        or result.execution_receipt is None
        or result.execution_receipt.fill is None
        or replay.projection_hash != result.projection_hash
        or service.event_count != len(result.events)
    ):
        raise ProbeError("paper cycle validation failed")
    return result.control_hash


def forecast_contract_once(index: int) -> str:
    _require_index(index)
    request = _forecast_request(index)
    forecast = _forecast_signal(index, request)
    output = ForecastOutputArtifact.model_validate(
        {
            "request_id": request.request_id,
            "forecast": forecast,
            "raw_output_artifact_ref": f"sha256:{'6' * 64}",
            "sampled_paths_artifact_ref": None,
            "model_artifact_hash": request.model_artifact_hash,
            "tokenizer_artifact_hash": request.tokenizer_artifact_hash,
            "runtime_hash": request.runtime_hash,
            "data_hash": request.data_hash,
            "stochastic": False,
            "created_at": FIXED_NOW + timedelta(seconds=1),
        },
        context={"request": request},
    )
    replayed = ForecastSignal.model_validate_json(forecast.canonical_json())
    if replayed.payload_hash() != forecast.payload_hash():
        raise ProbeError("forecast contract validation failed")
    return stable_payload_hash(output.model_dump(mode="json"))


def _forecast_request(index: int) -> ForecastRequest:
    identity = uuid5(NAMESPACE_URL, f"stonks:capacity:forecast:{index}")
    return ForecastRequest(
        request_id=identity,
        run_id=uuid5(identity, "run"),
        instrument_id=uuid5(identity, "instrument"),
        dataset_snapshot_id=uuid5(identity, "snapshot"),
        snapshot_artifact_ref=f"sha256:{'1' * 64}",
        data_hash="2" * 64,
        as_of=FIXED_NOW,
        interval="1d",
        horizon_bars=2,
        input_window_start=FIXED_NOW - timedelta(days=2),
        input_window_end=FIXED_NOW,
        model_id="Kronos-small",
        model_revision="capacity-fixture",
        model_artifact_hash="3" * 64,
        tokenizer_id="Kronos-Tokenizer-base",
        tokenizer_revision="capacity-fixture",
        tokenizer_artifact_hash="4" * 64,
        runtime_hash="5" * 64,
        requested_at=FIXED_NOW,
        deadline_at=FIXED_NOW + timedelta(minutes=1),
    )


def _forecast_signal(index: int, request: ForecastRequest) -> ForecastSignal:
    return ForecastSignal(
        forecast_id=uuid5(request.request_id, f"forecast:{index}"),
        instrument_id=request.instrument_id,
        as_of=request.as_of,
        interval=request.interval,
        horizon_bars=request.horizon_bars,
        expected_return=Decimal("0.02"),
        median_return=Decimal("0.01"),
        direction_probability=Decimal("0.60"),
        expected_volatility=Decimal("0.03"),
        downside_quantile=Decimal("-0.04"),
        max_drawdown_quantile=Decimal("-0.05"),
        path_count=3,
        dispersion=Decimal("0.02"),
        input_quality=DataQuality(
            status=DataQualityStatus.AVAILABLE,
            completeness=Decimal("1"),
        ),
        model_id=request.model_id,
        model_revision=request.model_revision,
        tokenizer_id=request.tokenizer_id,
        tokenizer_revision=request.tokenizer_revision,
        device="cpu",
        seed_policy="deterministic-capacity-fixture",
        inference_code_version="capacity-probe/1",
        dataset_snapshot_id=request.dataset_snapshot_id,
        input_window_start=request.input_window_start,
        input_window_end=request.input_window_end,
        generated_at=FIXED_NOW,
        latency_ms=0,
    )


def _require_index(index: int) -> None:
    if type(index) is not int or index < 0:
        raise ProbeError("sample identity is invalid")
