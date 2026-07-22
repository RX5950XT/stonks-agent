from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fixtures.service_credentials import (
    TEST_SERVICE_TOKEN,
    RecordingServiceCredentialProvider,
)
from pydantic import ValidationError

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.adapters.research.tradingagents_http import (
    TradingAgentsHttpAdapter,
    TradingAgentsWorkerPolicy,
    load_worker_policy,
)
from stonks_agent.domain.auth import Permission, ResourceKind
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.ports.service_credentials import ServiceReceiver
from stonks_contracts.common import ConfidenceCalibration
from stonks_contracts.research import AgentOpinion, AnalysisBundle
from stonks_contracts.tradingagents import (
    SignedEvidenceArtifact,
    TradingAgentsWorkerRequest,
    TradingAgentsWorkerResponse,
    TradingAgentsWorkerResult,
)

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 7, 13, 1, tzinfo=UTC)
REQUEST_ID = UUID("10000000-0000-4000-8000-000000000001")
RUN_ID = UUID("10000000-0000-4000-8000-000000000002")
JOB_ID = UUID("10000000-0000-4000-8000-000000000003")
INSTRUMENT_ID = UUID("10000000-0000-4000-8000-000000000004")
EVIDENCE_ID = UUID("10000000-0000-4000-8000-000000000005")
OPINION_ID = UUID("10000000-0000-4000-8000-000000000006")


def request(**overrides: object) -> TradingAgentsWorkerRequest:
    artifact_hash = "a" * 64
    values: dict[str, object] = {
        "request_id": REQUEST_ID,
        "run_id": RUN_ID,
        "job_id": JOB_ID,
        "attempt_generation": 3,
        "attempt_nonce": "nonce-secret",
        "profile": "paper",
        "instrument_id": INSTRUMENT_ID,
        "symbol": "AAPL",
        "as_of": NOW,
        "horizon": "20 trading days",
        "allowed_evidence_ids": (EVIDENCE_ID,),
        "evidence": (
            SignedEvidenceArtifact(
                evidence_id=EVIDENCE_ID,
                artifact_ref=f"sha256:{artifact_hash}",
                signed_url=(
                    f"http://artifact-service:8080/v1/artifacts/{artifact_hash}"
                    "?expires=1783905000&signature=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                ),
                expires_at=NOW + timedelta(minutes=5),
                available_at=NOW - timedelta(minutes=1),
                category="market",
                untrusted_content=True,
            ),
        ),
        "deadline": NOW + timedelta(minutes=2),
    }
    values.update(overrides)
    return TradingAgentsWorkerRequest.model_validate(values)


def worker_result() -> TradingAgentsWorkerResult:
    return TradingAgentsWorkerResult(
        analysis_bundle=AnalysisBundle(
            bundle_id=UUID("10000000-0000-4000-8000-000000000007"),
            run_id=RUN_ID,
            as_of=NOW,
            analyst_artifact_ids=(),
            opinion_ids=(OPINION_ID,),
            source_refs=(EVIDENCE_ID,),
            worker_version="tradingagents-worker/0.1.0",
        ),
        agent_opinion=AgentOpinion(
            opinion_id=OPINION_ID,
            instrument_id=INSTRUMENT_ID,
            as_of=NOW,
            horizon="20 trading days",
            recommendation="Hold",
            thesis="Evidence-scoped research opinion.",
            confidence=Decimal("0"),
            calibration=ConfidenceCalibration.UNCALIBRATED,
            evidence_refs=(EVIDENCE_ID,),
            producer="tradingagents-isolated-worker",
            model_version="01477f9afb7a47b849ed4c9259d3a9a4738d9fda",
        ),
    )


def response(**overrides: object) -> TradingAgentsWorkerResponse:
    result = worker_result()
    values: dict[str, object] = {
        "request_id": REQUEST_ID,
        "run_id": RUN_ID,
        "job_id": JOB_ID,
        "attempt_generation": 3,
        "attempt_nonce": "nonce-secret",
        "result_artifact_hash": result.payload_hash(),
        "result": result,
    }
    values.update(overrides)
    return TradingAgentsWorkerResponse.model_validate(values)


def policy(**overrides: object) -> TradingAgentsWorkerPolicy:
    values: dict[str, object] = {
        "policy_id": "test-worker-v1",
        "profile": "paper",
        "origin": "http://tradingagents-paper:8080",
        "artifact_origin": "http://artifact-service:8080",
        "endpoint": "/v1/analyze",
        "timeout_seconds": 5,
        "max_response_bytes": 1_048_576,
        "max_request_bytes": 1_048_576,
        "max_transient_retries": 0,
    }
    values.update(overrides)
    return TradingAgentsWorkerPolicy.model_validate(values)


def envelope(value: TradingAgentsWorkerResponse) -> dict[str, object]:
    return {
        "success": True,
        "status": 200,
        "data": value.model_dump(mode="json"),
        "error": None,
        "metadata": None,
    }


def adapter(
    handler: object,
    *,
    artifacts: MemoryArtifactStore | None = None,
    worker_policy: TradingAgentsWorkerPolicy | None = None,
    sleeper: object = lambda _: None,
    credentials: RecordingServiceCredentialProvider | None = None,
) -> tuple[TradingAgentsHttpAdapter, httpx.Client, MemoryArtifactStore]:
    store = artifacts or MemoryArtifactStore()
    client = httpx.Client(
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        follow_redirects=True,
    )
    value = TradingAgentsHttpAdapter(
        client=client,
        artifacts=store,
        policy=worker_policy or policy(),
        credentials=credentials or RecordingServiceCredentialProvider(),
        clock=lambda: NOW,
        monotonic_clock=lambda: 1.0,
        sleeper=sleeper,  # type: ignore[arg-type]
    )
    return value, client, store


def test_success_is_fixed_origin_ref_only_fenced_and_archived() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        payload = json.loads(incoming.content)
        assert incoming.url == "http://tradingagents-paper:8080/v1/analyze"
        assert incoming.headers["Accept-Encoding"] == "identity"
        assert incoming.headers["Authorization"] == f"Bearer {TEST_SERVICE_TOKEN}"
        assert incoming.extensions["timeout"]["read"] == 5
        assert all("content" not in item for item in payload["evidence"])
        assert payload["attempt_generation"] == 3
        assert payload["attempt_nonce"] == "nonce-secret"
        return httpx.Response(200, json=envelope(response()), request=incoming)

    subject, client, store = adapter(handler)
    with client:
        result = subject.analyze(request())

    assert isinstance(result, Success)
    assert result.value.response.result.analysis_bundle.run_id == RUN_ID
    assert result.value.artifact.content_hash == response().result_artifact_hash
    assert store.is_finalized(response().result_artifact_hash)


def test_credential_failure_denies_dispatch_before_network() -> None:
    credentials = RecordingServiceCredentialProvider(available=False)
    subject, client, _ = adapter(
        lambda _request: pytest.fail("network must not be called"),
        credentials=credentials,
    )

    with client:
        result = subject.analyze(request())

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.UNAUTHORIZED
    assert len(credentials.requests) == 1
    issued = credentials.requests[0]
    assert issued.receiver is ServiceReceiver.TRADINGAGENTS
    assert issued.permission is Permission.DISPATCH_ASSIGNED_RESEARCH
    assert issued.target.kind is ResourceKind.JOB
    assert issued.target.identifier == str(JOB_ID)
    assert issued.attempt_generation == request().attempt_generation
    assert issued.expires_no_later_than == request().deadline


def test_fence_mismatch_and_schema_drift_fail_before_archive() -> None:
    cases = []
    stale = envelope(response())
    stale["data"]["attempt_nonce"] = "late-nonce"  # type: ignore[index]
    cases.append((stale, ErrorCode.CONFLICT))
    drift = envelope(response())
    drift["data"]["order"] = {"quantity": 100}  # type: ignore[index]
    cases.append((drift, ErrorCode.MODEL_OUTPUT_INVALID))

    for payload, expected in cases:
        store = MemoryArtifactStore()
        subject, client, _ = adapter(
            lambda incoming, payload=payload: httpx.Response(
                200, json=payload, request=incoming
            ),
            artifacts=store,
        )
        with client:
            result = subject.analyze(request())
        assert isinstance(result, Failure)
        assert result.error.code is expected
        assert not store.is_finalized(response().result_artifact_hash)


def test_tampered_result_hash_fails_closed() -> None:
    payload = envelope(response())
    payload["data"]["result_artifact_hash"] = "f" * 64  # type: ignore[index]
    subject, client, _ = adapter(
        lambda incoming: httpx.Response(200, json=payload, request=incoming)
    )
    with client:
        result = subject.analyze(request())
    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.MODEL_OUTPUT_INVALID


def test_profile_artifact_origin_expiry_and_nested_result_context_are_fenced() -> None:
    calls: list[int] = []
    subject, client, _ = adapter(
        lambda incoming: (
            calls.append(1),
            httpx.Response(200, json=envelope(response()), request=incoming),
        )[1]
    )
    bad_capability = (
        request()
        .evidence[0]
        .model_copy(
            update={
                "signed_url": request()
                .evidence[0]
                .signed_url.replace("artifact-service:8080", "attacker:8080")
            }
        )
    )
    with client:
        denied = subject.analyze(request(evidence=(bad_capability,)))
    assert isinstance(denied, Failure)
    assert denied.error.code is ErrorCode.CAPABILITY_DENIED
    assert calls == []

    mismatched_result = worker_result().model_copy(
        update={
            "analysis_bundle": worker_result().analysis_bundle.model_copy(
                update={"run_id": UUID(int=999)}
            )
        }
    )
    mismatched = response(
        result=mismatched_result,
        result_artifact_hash=mismatched_result.payload_hash(),
    )
    subject, client, _ = adapter(
        lambda incoming: httpx.Response(
            200, json=envelope(mismatched), request=incoming
        )
    )
    with client:
        rejected = subject.analyze(request())
    assert isinstance(rejected, Failure)
    assert rejected.error.code is ErrorCode.CONFLICT


def test_transient_retry_is_bounded_and_permanent_failure_is_not_retried() -> None:
    calls: list[int] = []

    def transient(incoming: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, request=incoming)
        return httpx.Response(200, json=envelope(response()), request=incoming)

    subject, client, _ = adapter(
        transient, worker_policy=policy(max_transient_retries=1)
    )
    with client:
        result = subject.analyze(request())
    assert isinstance(result, Success)
    assert len(calls) == 2

    calls.clear()
    subject, client, _ = adapter(
        lambda incoming: (calls.append(1), httpx.Response(400, request=incoming))[1],
        worker_policy=policy(max_transient_retries=2),
    )
    with client:
        result = subject.analyze(request())
    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INVALID_INPUT
    assert len(calls) == 1


def test_worker_busy_is_rate_limited_without_retry() -> None:
    calls: list[int] = []
    subject, client, _ = adapter(
        lambda incoming: (
            calls.append(1),
            httpx.Response(429, request=incoming),
        )[1],
        worker_policy=policy(max_transient_retries=5),
    )

    with client:
        result = subject.analyze(request())

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.RATE_LIMITED
    assert calls == [1]


@pytest.mark.parametrize(
    ("headers", "body", "expected"),
    [
        ({"content-type": "text/html"}, b"{}", ErrorCode.MODEL_OUTPUT_INVALID),
        (
            {"content-type": "application/json", "content-encoding": "gzip"},
            b"{}",
            ErrorCode.MODEL_OUTPUT_INVALID,
        ),
        (
            {"content-type": "application/json"},
            b"x" * 257,
            ErrorCode.PAYLOAD_TOO_LARGE,
        ),
    ],
)
def test_content_type_encoding_and_response_size_are_bounded(
    headers: dict[str, str], body: bytes, expected: ErrorCode
) -> None:
    subject, client, _ = adapter(
        lambda incoming: httpx.Response(
            200, headers=headers, content=body, request=incoming
        ),
        worker_policy=policy(max_response_bytes=256),
    )
    with client:
        result = subject.analyze(request())
    assert isinstance(result, Failure)
    assert result.error.code is expected


def test_policy_and_signed_capability_validation_fail_closed() -> None:
    loaded = load_worker_policy(str(ROOT / "config/workers/tradingagents.yaml"))
    assert loaded.endpoint == "/v1/analyze"
    assert loaded.origin == "http://tradingagents-paper:7100"
    assert loaded.max_transient_retries == 1
    with pytest.raises(ValidationError):
        policy(origin="http://user:password@attacker.example")
    with pytest.raises(ValidationError):
        SignedEvidenceArtifact.model_validate(
            {
                **request().evidence[0].model_dump(mode="python"),
                "signed_url": "https://attacker.example/no-signature",
            }
        )
