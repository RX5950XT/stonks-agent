"""Calendar-aware, artifact-first adapter for the isolated Kronos worker."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stonks_agent.adapters.market_data._http_response import (
    ResponseBodyError,
    read_bounded_raw,
    response_deadline,
)
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.calendar import ExchangeCalendar
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.signal import ForecastOutputArtifact, ForecastRequest
from stonks_agent.ports.artifact_store import ArtifactManifest, ArtifactStore
from stonks_contracts.evidence import Sensitivity
from stonks_contracts.kronos import (
    KronosBar,
    KronosForecastPath,
    KronosRuntimeIdentity,
    KronosSamplePathsArtifact,
    KronosSamplingPolicy,
    KronosWorkerRequest,
    KronosWorkerResponse,
    VolumeQuality,
)
from stonks_contracts.market_data import (
    Bar,
    BarSeries,
    DataQuality,
    DataQualityStatus,
)
from stonks_contracts.signal import ForecastSignal

_TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class KronosHttpPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    profile: Literal["cpu", "cuda"]
    origin: str
    endpoint: str = Field(pattern=r"^/v[0-9]+/[a-z0-9/-]+$")
    timeout_seconds: float = Field(gt=0, le=300)
    max_response_bytes: int = Field(ge=1, le=16_777_216)
    max_request_bytes: int = Field(ge=1, le=16_777_216)
    max_transient_retries: int = Field(ge=0, le=5)
    max_absolute_step_return: Decimal = Field(gt=0, le=10)

    @model_validator(mode="after")
    def validate_origin(self) -> Self:
        if not _valid_origin(self.origin):
            raise ValueError("worker origin is invalid")
        return self


class KronosWorkerConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: KronosHttpPolicy
    runtime: KronosRuntimeIdentity

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        if self.policy.profile != self.runtime.device:
            raise ValueError("worker policy and runtime profiles must match")
        return self


def load_kronos_worker_configuration(path: str | Path) -> KronosWorkerConfiguration:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return KronosWorkerConfiguration.model_validate(payload)


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


def build_kronos_worker_request(
    request: ForecastRequest,
    *,
    job_id: UUID,
    attempt_generation: int,
    attempt_nonce: str,
    mic: str,
    series: BarSeries,
    calendar: ExchangeCalendar,
    runtime: KronosRuntimeIdentity,
    sampling: KronosSamplingPolicy,
    volume_quality: VolumeQuality,
) -> Result[KronosWorkerRequest]:
    """Bind canonical PIT bars and exchange session closes to a worker lease."""
    mismatch = _builder_mismatch(request, mic, series, calendar, runtime)
    if mismatch is not None:
        return mismatch
    if request.interval != "1d" or series.interval != "1d":
        return _failure(ErrorCode.INVALID_INPUT, "Kronos requires daily bars")
    if request.horizon_bars > 256:
        return _failure(ErrorCode.INVALID_INPUT, "Kronos horizon is too large")
    try:
        bars = tuple(_to_worker_bar(bar, volume_quality) for bar in series.bars)
        future_timestamps = _future_session_closes(
            calendar, request.as_of, request.horizon_bars
        )
        worker_request = KronosWorkerRequest(
            request_id=request.request_id,
            run_id=request.run_id,
            job_id=job_id,
            attempt_generation=attempt_generation,
            attempt_nonce=attempt_nonce,
            profile=runtime.device,
            instrument_id=request.instrument_id,
            mic=mic,
            dataset_snapshot_id=request.dataset_snapshot_id,
            snapshot_artifact_ref=request.snapshot_artifact_ref,
            data_hash=request.data_hash,
            as_of=request.as_of,
            interval="1d",
            bars=bars,
            future_timestamps=future_timestamps,
            runtime=runtime,
            sampling=sampling,
            deadline=request.deadline_at,
        )
    except (LookupError, ValidationError, ValueError):
        return _failure(ErrorCode.INVALID_INPUT, "Kronos request is invalid")
    return Success(worker_request)


def _builder_mismatch(
    request: ForecastRequest,
    mic: str,
    series: BarSeries,
    calendar: ExchangeCalendar,
    runtime: KronosRuntimeIdentity,
) -> Failure | None:
    if mic != calendar.mic:
        return _failure(ErrorCode.CONFLICT, "Calendar MIC does not match")
    if (
        series.instrument_id != request.instrument_id
        or series.as_of != request.as_of
        or series.interval != request.interval
        or not series.bars
        or series.bars[0].event_time != request.input_window_start
        or series.bars[-1].event_time != request.input_window_end
    ):
        return _failure(
            ErrorCode.CONFLICT, "Bar series does not match forecast request"
        )
    if not _runtime_matches_request(runtime, request):
        return _failure(ErrorCode.CONFLICT, "Runtime does not match forecast request")
    return None


def _to_worker_bar(bar: Bar, quality: VolumeQuality) -> KronosBar:
    volume = None if quality is VolumeQuality.MISSING else bar.volume
    amount = None if quality is VolumeQuality.MISSING else bar.amount
    return KronosBar(
        event_time=bar.event_time,
        available_at=bar.available_at,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=volume,
        amount=amount,
        volume_quality=quality,
    )


def _future_session_closes(
    calendar: ExchangeCalendar, as_of: datetime, horizon: int
) -> tuple[datetime, ...]:
    closes: list[datetime] = []
    cursor = as_of
    for _ in range(horizon):
        session = calendar.next_session_after(cursor)
        closes.append(session.closes_at)
        cursor = session.closes_at
    return tuple(closes)


class KronosHttpAdapter:
    __slots__ = (
        "_artifacts",
        "_client",
        "_clock",
        "_monotonic",
        "_policy",
        "_sleep",
    )

    def __init__(
        self,
        *,
        client: httpx.Client,
        artifacts: ArtifactStore,
        policy: KronosHttpPolicy,
        clock: Callable[[], datetime],
        monotonic_clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self._client = client
        self._artifacts = artifacts
        self._policy = policy
        self._clock = clock
        self._monotonic = monotonic_clock
        self._sleep = sleeper

    def forecast(
        self, worker_request: KronosWorkerRequest, request: ForecastRequest
    ) -> Result[ForecastOutputArtifact]:
        mismatch = _request_pair_mismatch(worker_request, request, self._policy)
        if mismatch is not None:
            return mismatch
        content = worker_request.canonical_json().encode("utf-8")
        if len(content) > self._policy.max_request_bytes:
            return _failure(ErrorCode.PAYLOAD_TOO_LARGE, "Worker request is too large")
        raw = self._send(worker_request, content)
        if isinstance(raw, Failure):
            return raw
        raw_manifest = self._archive(
            raw.value,
            source="kronos-isolated-worker-raw-response",
            schema="kronos-worker-envelope/1.0.0",
        )
        if isinstance(raw_manifest, Failure):
            return raw_manifest
        parsed = self._parse_response(worker_request, raw.value)
        if isinstance(parsed, Failure):
            return parsed
        artifact = _sample_paths_artifact(parsed.value)
        paths_manifest = self._archive(
            artifact.canonical_json().encode("utf-8"),
            source="kronos-sample-paths",
            schema="kronos-sample-paths/1.0.0",
        )
        if isinstance(paths_manifest, Failure):
            return paths_manifest
        response_mismatch = _response_mismatch(worker_request, parsed.value)
        if response_mismatch is not None:
            return response_mismatch
        return _map_forecast(
            request,
            artifact,
            raw_output_artifact_ref=f"sha256:{raw_manifest.value.content_hash}",
            sampled_paths_artifact_ref=f"sha256:{paths_manifest.value.content_hash}",
            policy=self._policy,
        )

    def _send(self, request: KronosWorkerRequest, content: bytes) -> Result[bytes]:
        for retry in range(self._policy.max_transient_retries + 1):
            now = self._clock()
            remaining = (request.deadline - now).total_seconds()
            if now.tzinfo is None or remaining <= 0:
                return _failure(ErrorCode.DEADLINE_EXCEEDED, "Worker deadline exceeded")
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
                        return _status_failure(response.status_code)
                    if _media_type(response) != "application/json":
                        return _invalid_response()
                    body = read_bounded_raw(
                        response,
                        max_bytes=self._policy.max_response_bytes,
                        deadline=deadline,
                        clock=self._monotonic,
                    )
                    if isinstance(body, ResponseBodyError):
                        return _body_failure(body)
                    return Success(body)
            except httpx.DecodingError:
                return _invalid_response()
            except httpx.HTTPError:
                if retry < self._policy.max_transient_retries:
                    self._backoff(retry, request.deadline)
                    continue
                return _failure(ErrorCode.DATA_UNAVAILABLE, "Worker is unavailable")
        return _failure(ErrorCode.DATA_UNAVAILABLE, "Worker is unavailable")

    def _parse_response(
        self, request: KronosWorkerRequest, body: bytes
    ) -> Result[KronosWorkerResponse]:
        if self._clock() >= request.deadline:
            return _failure(ErrorCode.DEADLINE_EXCEEDED, "Worker deadline exceeded")
        try:
            envelope = _WorkerEnvelope.model_validate_json(body)
            if not envelope.success or envelope.status != 200 or envelope.data is None:
                return _invalid_response()
            response = KronosWorkerResponse.model_validate(envelope.data)
        except (ValidationError, ValueError):
            return _invalid_response()
        return Success(response)

    def _archive(
        self, content: bytes, *, source: str, schema: str
    ) -> Result[ArtifactManifest]:
        return self._artifacts.finalize(
            content,
            metadata=ArtifactMetadata(
                media_type="application/json",
                license_tag="LicenseRef-Kronos-Generated-Output",
                sensitivity=Sensitivity.INTERNAL,
                source=source,
                attributes=(("schema", schema),),
            ),
            finalized_at=self._clock(),
        )

    def _backoff(self, retry: int, deadline: datetime) -> None:
        delay = 0.25 * (2**retry)
        if self._clock() + timedelta(seconds=delay) < deadline:
            self._sleep(delay)


def replay_kronos_forecast(
    request: ForecastRequest,
    *,
    raw_output_artifact_ref: str,
    sampled_paths_artifact_ref: str,
    artifacts: ArtifactStore,
    policy: KronosHttpPolicy,
) -> Result[ForecastOutputArtifact]:
    """Replay deterministic mapping without invoking stochastic inference."""
    content_hash = sampled_paths_artifact_ref.removeprefix("sha256:")
    stored = artifacts.read(content_hash)
    if isinstance(stored, Failure):
        return stored
    try:
        artifact = KronosSamplePathsArtifact.model_validate_json(stored.value)
    except ValidationError:
        return _failure(ErrorCode.CONFLICT, "Sample paths artifact is invalid")
    return _map_forecast(
        request,
        artifact,
        raw_output_artifact_ref=raw_output_artifact_ref,
        sampled_paths_artifact_ref=sampled_paths_artifact_ref,
        policy=policy,
    )


def _sample_paths_artifact(response: KronosWorkerResponse) -> KronosSamplePathsArtifact:
    result = response.result
    return KronosSamplePathsArtifact(
        request_id=response.request_id,
        instrument_id=result.instrument_id,
        dataset_snapshot_id=result.dataset_snapshot_id,
        as_of=result.as_of,
        interval=result.interval,
        input_window_start=result.input_window_start,
        input_window_end=result.input_window_end,
        input_last_close=result.input_last_close,
        input_volume_quality=result.input_volume_quality,
        runtime=result.runtime,
        sampling=result.sampling,
        future_timestamps=result.future_timestamps,
        paths=result.paths,
        generated_at=result.generated_at,
        latency_ms=result.latency_ms,
        warnings=result.warnings,
    )


def _map_forecast(
    request: ForecastRequest,
    artifact: KronosSamplePathsArtifact,
    *,
    raw_output_artifact_ref: str,
    sampled_paths_artifact_ref: str,
    policy: KronosHttpPolicy,
) -> Result[ForecastOutputArtifact]:
    if not _artifact_matches_request(artifact, request, policy):
        return _failure(ErrorCode.CONFLICT, "Sample paths do not match request")
    path_validation = _validate_path_jumps(artifact, policy.max_absolute_step_return)
    if path_validation is not None:
        return path_validation
    terminal_returns = tuple(
        path.points[-1].close / artifact.input_last_close - 1 for path in artifact.paths
    )
    step_returns = tuple(
        value
        for path in artifact.paths
        for value in _path_step_returns(path, artifact.input_last_close)
    )
    drawdowns = tuple(
        _max_drawdown(path, artifact.input_last_close) for path in artifact.paths
    )
    path_hash = artifact.payload_hash()
    quality = _input_quality(artifact.input_volume_quality)
    signal = ForecastSignal(
        forecast_id=uuid5(NAMESPACE_URL, f"kronos:{request.request_id}:{path_hash}"),
        instrument_id=request.instrument_id,
        as_of=request.as_of,
        interval=request.interval,
        horizon_bars=request.horizon_bars,
        expected_return=_mean(terminal_returns),
        median_return=_quantile(terminal_returns, Decimal("0.5")),
        direction_probability=(
            Decimal(sum(value > 0 for value in terminal_returns))
            / Decimal(len(terminal_returns))
        ),
        expected_volatility=_population_stddev(step_returns),
        downside_quantile=_quantile(terminal_returns, Decimal("0.05")),
        max_drawdown_quantile=_quantile(drawdowns, Decimal("0.05")),
        path_count=len(artifact.paths),
        dispersion=_population_stddev(terminal_returns),
        input_quality=quality,
        model_id=artifact.runtime.model_id,
        model_revision=artifact.runtime.model_revision,
        tokenizer_id=artifact.runtime.tokenizer_id,
        tokenizer_revision=artifact.runtime.tokenizer_revision,
        device=artifact.runtime.device,
        seed_policy=artifact.sampling.seed_policy,
        inference_code_version=artifact.runtime.inference_code_version,
        dataset_snapshot_id=request.dataset_snapshot_id,
        input_window_start=request.input_window_start,
        input_window_end=request.input_window_end,
        generated_at=artifact.generated_at,
        latency_ms=artifact.latency_ms,
        validity_warnings=artifact.warnings,
    )
    payload = {
        "request_id": request.request_id,
        "forecast": signal,
        "raw_output_artifact_ref": raw_output_artifact_ref,
        "sampled_paths_artifact_ref": sampled_paths_artifact_ref,
        "model_artifact_hash": artifact.runtime.model_artifact_hash,
        "tokenizer_artifact_hash": artifact.runtime.tokenizer_artifact_hash,
        "runtime_hash": artifact.runtime.runtime_hash,
        "data_hash": request.data_hash,
        "stochastic": True,
        "created_at": artifact.generated_at,
    }
    try:
        output = ForecastOutputArtifact.model_validate(
            payload, context={"request": request}
        )
    except ValidationError:
        return _failure(ErrorCode.MODEL_OUTPUT_INVALID, "Forecast mapping is invalid")
    return Success(output)


def _runtime_matches_request(
    runtime: KronosRuntimeIdentity, request: ForecastRequest
) -> bool:
    return (
        runtime.model_id == request.model_id
        and runtime.model_revision == request.model_revision
        and runtime.model_artifact_hash == request.model_artifact_hash
        and runtime.tokenizer_id == request.tokenizer_id
        and runtime.tokenizer_revision == request.tokenizer_revision
        and runtime.tokenizer_artifact_hash == request.tokenizer_artifact_hash
        and runtime.runtime_hash == request.runtime_hash
    )


def _request_pair_mismatch(
    worker: KronosWorkerRequest,
    request: ForecastRequest,
    policy: KronosHttpPolicy,
) -> Failure | None:
    if worker.profile != policy.profile:
        return _failure(ErrorCode.CAPABILITY_DENIED, "Worker profile is unauthorized")
    if (
        worker.request_id != request.request_id
        or worker.run_id != request.run_id
        or worker.instrument_id != request.instrument_id
        or worker.dataset_snapshot_id != request.dataset_snapshot_id
        or worker.snapshot_artifact_ref != request.snapshot_artifact_ref
        or worker.data_hash != request.data_hash
        or worker.as_of != request.as_of
        or worker.interval != request.interval
        or worker.horizon_bars != request.horizon_bars
        or worker.bars[0].event_time != request.input_window_start
        or worker.bars[-1].event_time != request.input_window_end
        or worker.deadline != request.deadline_at
        or not _runtime_matches_request(worker.runtime, request)
    ):
        return _failure(ErrorCode.CONFLICT, "Worker request binding does not match")
    return None


def _lease_matches(
    request: KronosWorkerRequest, response: KronosWorkerResponse
) -> bool:
    return (
        response.request_id == request.request_id
        and response.run_id == request.run_id
        and response.job_id == request.job_id
        and response.attempt_generation == request.attempt_generation
        and response.attempt_nonce == request.attempt_nonce
    )


def _response_mismatch(
    request: KronosWorkerRequest, response: KronosWorkerResponse
) -> Failure | None:
    if not _lease_matches(request, response):
        return _failure(ErrorCode.CONFLICT, "Worker lease fence does not match")
    if not _result_matches_worker_request(request, response):
        return _failure(ErrorCode.CONFLICT, "Worker result context does not match")
    return None


def _result_matches_worker_request(
    request: KronosWorkerRequest, response: KronosWorkerResponse
) -> bool:
    result = response.result
    return (
        result.instrument_id == request.instrument_id
        and result.dataset_snapshot_id == request.dataset_snapshot_id
        and result.as_of == request.as_of
        and result.interval == request.interval
        and result.input_window_start == request.bars[0].event_time
        and result.input_window_end == request.bars[-1].event_time
        and result.future_timestamps == request.future_timestamps
        and result.input_last_close == request.bars[-1].close
        and result.input_volume_quality == _minimum_volume_quality(request)
        and result.runtime == request.runtime
        and result.sampling == request.sampling
    )


def _minimum_volume_quality(request: KronosWorkerRequest) -> VolumeQuality:
    qualities = {bar.volume_quality for bar in request.bars}
    if VolumeQuality.MISSING in qualities:
        return VolumeQuality.MISSING
    if VolumeQuality.ESTIMATED in qualities:
        return VolumeQuality.ESTIMATED
    return VolumeQuality.OBSERVED


def _artifact_matches_request(
    artifact: KronosSamplePathsArtifact,
    request: ForecastRequest,
    policy: KronosHttpPolicy,
) -> bool:
    return (
        artifact.request_id == request.request_id
        and artifact.instrument_id == request.instrument_id
        and artifact.dataset_snapshot_id == request.dataset_snapshot_id
        and artifact.as_of == request.as_of
        and artifact.interval == request.interval
        and artifact.input_window_start == request.input_window_start
        and artifact.input_window_end == request.input_window_end
        and len(artifact.future_timestamps) == request.horizon_bars
        and artifact.generated_at <= request.deadline_at
        and artifact.runtime.device == policy.profile
        and _runtime_matches_request(artifact.runtime, request)
    )


def _validate_path_jumps(
    artifact: KronosSamplePathsArtifact, maximum: Decimal
) -> Failure | None:
    for path in artifact.paths:
        previous = artifact.input_last_close
        for point in path.points:
            if abs(point.close / previous - 1) > maximum:
                return _failure(
                    ErrorCode.MODEL_OUTPUT_INVALID,
                    "Kronos path exceeds the configured jump bound",
                )
            previous = point.close
    return None


def _path_step_returns(
    path: KronosForecastPath, initial: Decimal
) -> tuple[Decimal, ...]:
    returns: list[Decimal] = []
    previous = initial
    for point in path.points:
        returns.append(point.close / previous - 1)
        previous = point.close
    return tuple(returns)


def _max_drawdown(path: KronosForecastPath, initial: Decimal) -> Decimal:
    peak = initial
    drawdown = Decimal(0)
    for point in path.points:
        peak = max(peak, point.close)
        drawdown = min(drawdown, point.close / peak - 1)
    return drawdown


def _input_quality(quality: VolumeQuality) -> DataQuality:
    if quality is VolumeQuality.MISSING:
        return DataQuality(
            status=DataQualityStatus.PARTIAL,
            completeness=Decimal("0.5"),
            warnings=("input_volume_missing",),
        )
    if quality is VolumeQuality.ESTIMATED:
        return DataQuality(
            status=DataQualityStatus.ESTIMATED,
            completeness=Decimal("0.8"),
            warnings=("input_volume_estimated",),
        )
    return DataQuality(
        status=DataQualityStatus.AVAILABLE,
        completeness=Decimal(1),
    )


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def _population_stddev(values: tuple[Decimal, ...]) -> Decimal:
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / Decimal(len(values))
    return variance.sqrt()


def _quantile(values: tuple[Decimal, ...], probability: Decimal) -> Decimal:
    ordered = sorted(values)
    position = Decimal(len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _media_type(response: httpx.Response) -> str:
    return str(response.headers.get("content-type", "")).split(";", 1)[0].strip()


def _valid_origin(value: str) -> bool:
    parsed = urlsplit(value)
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )


def _status_failure(status: int) -> Failure:
    if status in {401, 403}:
        return _failure(ErrorCode.UNAUTHORIZED, "Worker rejected the request")
    if status == 408:
        return _failure(ErrorCode.DEADLINE_EXCEEDED, "Worker deadline exceeded")
    if status == 413:
        return _failure(ErrorCode.PAYLOAD_TOO_LARGE, "Worker rejected request size")
    if status in {400, 409, 422}:
        return _failure(ErrorCode.INVALID_INPUT, "Worker rejected the request")
    if status == 429:
        return _failure(ErrorCode.RATE_LIMITED, "Worker rate limit exceeded")
    return _failure(ErrorCode.DATA_UNAVAILABLE, "Worker is unavailable")


def _body_failure(error: ResponseBodyError) -> Failure:
    if error is ResponseBodyError.DEADLINE_EXCEEDED:
        return _failure(
            ErrorCode.DEADLINE_EXCEEDED, "Worker response deadline exceeded"
        )
    if error is ResponseBodyError.RESPONSE_TOO_LARGE:
        return _failure(ErrorCode.PAYLOAD_TOO_LARGE, "Worker response is too large")
    return _invalid_response()


def _invalid_response() -> Failure:
    return _failure(ErrorCode.MODEL_OUTPUT_INVALID, "Worker response is invalid")


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
