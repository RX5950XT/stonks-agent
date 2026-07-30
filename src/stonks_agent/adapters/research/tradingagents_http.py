"""Fixed-origin core adapter for the isolated TradingAgents worker."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta
from time import monotonic, sleep
from typing import Self
from urllib.parse import urlsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stonks_agent.adapters._worker_http import (
    body_failure,
    invalid_response,
    status_failure,
    valid_origin,
    worker_failure,
)
from stonks_agent.adapters.market_data._http_response import (
    ResponseBodyError,
    read_bounded_raw,
    response_deadline,
)
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.auth import AccessTarget, Permission, ResourceKind
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    Success,
)
from stonks_agent.ports.artifact_store import ArtifactManifest, ArtifactStore
from stonks_agent.ports.service_credentials import (
    ServiceCredentialProvider,
    ServiceCredentialRequest,
    ServiceReceiver,
)
from stonks_contracts.evidence import Sensitivity
from stonks_contracts.tradingagents import (
    TradingAgentsWorkerRequest,
    TradingAgentsWorkerResponse,
)

_TRANSIENT_STATUSES = frozenset({408, 500, 502, 503, 504})


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class TradingAgentsWorkerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    profile: str = Field(pattern=r"^(paper|backtest|production)$")
    origin: str
    artifact_origin: str
    endpoint: str = Field(pattern=r"^/v[0-9]+/[a-z0-9/-]+$")
    timeout_seconds: float = Field(gt=0, le=300)
    max_response_bytes: int = Field(ge=1, le=16_777_216)
    max_request_bytes: int = Field(ge=1, le=16_777_216)
    max_concurrency: int = Field(default=1, strict=True, ge=1, le=1)
    max_transient_retries: int = Field(ge=0, le=5)

    @model_validator(mode="after")
    def validate_fixed_origin(self) -> Self:
        if not valid_origin(self.origin) or not valid_origin(self.artifact_origin):
            raise ValueError("worker origin is invalid")
        return self


class TradingAgentsResultReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    response: TradingAgentsWorkerResponse
    artifact: ArtifactManifest


class _WorkerError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: str = Field(min_length=1, max_length=256)


class _WorkerEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    success: bool
    status: int = Field(ge=100, le=599)
    data: dict[str, object] | None
    error: _WorkerError | None
    metadata: None

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.success != (self.error is None) or self.success != (
            self.data is not None
        ):
            raise ValueError("worker envelope is inconsistent")
        return self


def load_worker_policy(path: str) -> TradingAgentsWorkerPolicy:
    with open(path, encoding="utf-8") as source:
        payload = yaml.safe_load(source)
    return TradingAgentsWorkerPolicy.model_validate(payload)


class TradingAgentsHttpAdapter:
    __slots__ = (
        "_artifacts",
        "_client",
        "_clock",
        "_credentials",
        "_monotonic",
        "_policy",
        "_sleep",
    )

    def __init__(
        self,
        *,
        client: httpx.Client,
        artifacts: ArtifactStore,
        policy: TradingAgentsWorkerPolicy,
        credentials: ServiceCredentialProvider,
        clock: Callable[[], datetime],
        monotonic_clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self._client = client
        self._artifacts = artifacts
        self._policy = policy
        self._credentials = credentials
        self._clock = clock
        self._monotonic = monotonic_clock
        self._sleep = sleeper

    def analyze(
        self, request: TradingAgentsWorkerRequest
    ) -> Result[TradingAgentsResultReceipt]:
        scope_failure = self._validate_request_scope(request)
        if scope_failure is not None:
            return scope_failure
        content = request.canonical_json().encode("utf-8")
        if len(content) > self._policy.max_request_bytes:
            return worker_failure(
                ErrorCode.PAYLOAD_TOO_LARGE, "Worker request is too large"
            )
        raw = self._send(request, content)
        if isinstance(raw, Failure):
            return raw
        parsed = self._parse_response(request, raw.value)
        if isinstance(parsed, Failure):
            return parsed
        return self._archive(parsed.value)

    def _validate_request_scope(
        self, request: TradingAgentsWorkerRequest
    ) -> Failure | None:
        now = self._clock()
        if now.tzinfo is None or request.profile != self._policy.profile:
            return worker_failure(
                ErrorCode.CAPABILITY_DENIED, "Worker profile is not authorized"
            )
        expected_origin = self._policy.artifact_origin.rstrip("/")
        for item in request.evidence:
            parsed = urlsplit(item.signed_url)
            actual_origin = f"{parsed.scheme}://{parsed.netloc}"
            artifact_hash = item.artifact_ref.removeprefix("sha256:")
            if (
                actual_origin != expected_origin
                or parsed.path != f"/v1/artifacts/{artifact_hash}"
                or item.expires_at <= now
                or item.expires_at < request.deadline
            ):
                return worker_failure(
                    ErrorCode.CAPABILITY_DENIED,
                    "Evidence artifact capability is not authorized",
                )
        return None

    def _send(
        self, request: TradingAgentsWorkerRequest, content: bytes
    ) -> Result[bytes]:
        for retry in range(self._policy.max_transient_retries + 1):
            now = self._clock()
            remaining = (request.deadline - now).total_seconds()
            if now.tzinfo is None or remaining <= 0:
                return worker_failure(
                    ErrorCode.DEADLINE_EXCEEDED, "Worker deadline exceeded"
                )
            credential = self._credentials.issue(
                ServiceCredentialRequest(
                    receiver=ServiceReceiver.TRADINGAGENTS,
                    permission=Permission.DISPATCH_ASSIGNED_RESEARCH,
                    target=AccessTarget(
                        kind=ResourceKind.JOB,
                        identifier=str(request.job_id),
                    ),
                    request_id=request.request_id,
                    run_id=request.run_id,
                    attempt_generation=request.attempt_generation,
                    attempt_nonce_hash=_sha256_text(request.attempt_nonce),
                    request_hash=hashlib.sha256(content).hexdigest(),
                    expires_no_later_than=request.deadline,
                )
            )
            if isinstance(credential, Failure):
                return credential
            timeout = min(self._policy.timeout_seconds, remaining)
            deadline = response_deadline(self._monotonic, timeout)
            try:
                with self._client.stream(
                    "POST",
                    f"{self._policy.origin.rstrip('/')}{self._policy.endpoint}",
                    content=content,
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "Authorization": credential.value.authorization_header(),
                        "Content-Type": "application/json",
                    },
                    timeout=httpx.Timeout(timeout),
                    follow_redirects=False,
                ) as response:
                    if response.status_code != 200:
                        if (
                            response.status_code in _TRANSIENT_STATUSES
                            and retry < self._policy.max_transient_retries
                        ):
                            self._backoff(retry, request.deadline)
                            continue
                        return status_failure(response.status_code)
                    if (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        != "application/json"
                    ):
                        return invalid_response()
                    body = read_bounded_raw(
                        response,
                        max_bytes=self._policy.max_response_bytes,
                        deadline=deadline,
                        clock=self._monotonic,
                    )
                    if isinstance(body, ResponseBodyError):
                        return body_failure(body)
                    return Success(body)
            except httpx.DecodingError:
                return invalid_response()
            except httpx.HTTPError:
                if retry < self._policy.max_transient_retries:
                    self._backoff(retry, request.deadline)
                    continue
                return worker_failure(
                    ErrorCode.DATA_UNAVAILABLE, "Worker is unavailable"
                )
        return worker_failure(ErrorCode.DATA_UNAVAILABLE, "Worker is unavailable")

    def _parse_response(
        self, request: TradingAgentsWorkerRequest, body: bytes
    ) -> Result[TradingAgentsWorkerResponse]:
        if self._clock() >= request.deadline:
            return worker_failure(
                ErrorCode.DEADLINE_EXCEEDED, "Worker deadline exceeded"
            )
        try:
            envelope = _WorkerEnvelope.model_validate_json(body)
            if not envelope.success or envelope.status != 200 or envelope.data is None:
                return invalid_response()
            response = TradingAgentsWorkerResponse.model_validate(envelope.data)
        except (ValidationError, ValueError):
            return invalid_response()
        if (
            response.request_id != request.request_id
            or response.run_id != request.run_id
            or response.job_id != request.job_id
            or response.attempt_generation != request.attempt_generation
            or response.attempt_nonce != request.attempt_nonce
        ):
            return worker_failure(
                ErrorCode.CONFLICT, "Worker lease fence does not match"
            )
        if not _result_matches_request(request, response):
            return worker_failure(
                ErrorCode.CONFLICT, "Worker result context does not match"
            )
        return Success(response)

    def _archive(
        self, response: TradingAgentsWorkerResponse
    ) -> Result[TradingAgentsResultReceipt]:
        content = response.result.canonical_json().encode("utf-8")
        archived = self._artifacts.finalize(
            content,
            metadata=ArtifactMetadata(
                media_type="application/json",
                license_tag="Apache-2.0",
                sensitivity=Sensitivity.INTERNAL,
                source="tradingagents-isolated-worker",
                attributes=(("schema", "tradingagents-worker-result/1.0.0"),),
            ),
            finalized_at=self._clock(),
        )
        if isinstance(archived, Failure):
            return archived
        if archived.value.content_hash != response.result_artifact_hash:
            return worker_failure(
                ErrorCode.CONFLICT, "Worker result artifact hash does not match"
            )
        return Success(
            TradingAgentsResultReceipt(response=response, artifact=archived.value)
        )

    def _backoff(self, retry: int, deadline: datetime) -> None:
        delay = 0.25 * (2**retry)
        if self._clock() + timedelta(seconds=delay) < deadline:
            self._sleep(delay)


def _result_matches_request(
    request: TradingAgentsWorkerRequest,
    response: TradingAgentsWorkerResponse,
) -> bool:
    result = response.result
    bundle = result.analysis_bundle
    opinion = result.agent_opinion
    allowed = set(request.allowed_evidence_ids)
    return (
        bundle.run_id == request.run_id
        and bundle.as_of == request.as_of
        and opinion.opinion_id in bundle.opinion_ids
        and opinion.instrument_id == request.instrument_id
        and opinion.as_of == request.as_of
        and opinion.horizon == request.horizon
        and set(bundle.source_refs) <= allowed
        and set(opinion.evidence_refs) <= allowed
    )
