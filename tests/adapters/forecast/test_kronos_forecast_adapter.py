from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
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
from stonks_agent.adapters.forecast.kronos import (
    KronosHttpAdapter,
    KronosHttpPolicy,
    build_kronos_worker_request,
    load_kronos_worker_configuration,
    replay_kronos_forecast,
)
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.auth import Permission, ResourceKind
from stonks_agent.domain.calendar import ExchangeCalendar, SessionTemplate
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.signal import ForecastRequest
from stonks_agent.ports.service_credentials import ServiceReceiver
from stonks_contracts.evidence import Sensitivity
from stonks_contracts.kronos import (
    KronosForecastPath,
    KronosForecastPoint,
    KronosRuntimeIdentity,
    KronosSamplingPolicy,
    KronosWorkerRequest,
    KronosWorkerResponse,
    KronosWorkerResult,
    VolumeQuality,
)
from stonks_contracts.market_data import (
    Bar,
    BarSeries,
    DataQuality,
    DataQualityStatus,
)
from stonks_contracts.signal import ForecastSignal

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "tests" / "golden" / "kronos"
NOW = datetime(2026, 3, 6, 21, tzinfo=UTC)
REQUEST_ID = UUID("10000000-0000-4000-8000-000000000101")
RUN_ID = UUID("10000000-0000-4000-8000-000000000102")
JOB_ID = UUID("10000000-0000-4000-8000-000000000103")
INSTRUMENT_ID = UUID("10000000-0000-4000-8000-000000000104")
SNAPSHOT_ID = UUID("10000000-0000-4000-8000-000000000105")
MODEL_REVISION = "901c26c1332695a2a8f243eb2f37243a37bea320"
TOKENIZER_REVISION = "0e0117387f39004a9016484a186a908917e22426"
MODEL_HASH = "b082dfcbd8e8c142a725c8bbb99781802f38fec81210e13479effb32b3c3e020"
TOKENIZER_HASH = "59d85f6af76a2c3b8240ea06cb21db4213b4eeca053f246b23e29cf832fc6bee"
RUNTIME_HASH = "1" * 64


def request(**overrides: object) -> ForecastRequest:
    values: dict[str, object] = {
        "request_id": REQUEST_ID,
        "run_id": RUN_ID,
        "instrument_id": INSTRUMENT_ID,
        "dataset_snapshot_id": SNAPSHOT_ID,
        "snapshot_artifact_ref": f"sha256:{'a' * 64}",
        "data_hash": "b" * 64,
        "as_of": NOW,
        "interval": "1d",
        "horizon_bars": 2,
        "input_window_start": datetime(2026, 3, 4, 21, tzinfo=UTC),
        "input_window_end": NOW,
        "model_id": "NeoQuasar/Kronos-small",
        "model_revision": MODEL_REVISION,
        "model_artifact_hash": MODEL_HASH,
        "tokenizer_id": "NeoQuasar/Kronos-Tokenizer-base",
        "tokenizer_revision": TOKENIZER_REVISION,
        "tokenizer_artifact_hash": TOKENIZER_HASH,
        "runtime_hash": RUNTIME_HASH,
        "requested_at": NOW,
        "deadline_at": NOW + timedelta(minutes=5),
    }
    values.update(overrides)
    return ForecastRequest.model_validate(values)


def runtime(**overrides: object) -> KronosRuntimeIdentity:
    values: dict[str, object] = {
        "worker_version": "kronos-worker/0.2.0",
        "upstream_commit": "67b630e67f6a18c9e9be918d9b4337c960db1e9a",
        "model_id": "NeoQuasar/Kronos-small",
        "model_revision": MODEL_REVISION,
        "model_artifact_hash": MODEL_HASH,
        "tokenizer_id": "NeoQuasar/Kronos-Tokenizer-base",
        "tokenizer_revision": TOKENIZER_REVISION,
        "tokenizer_artifact_hash": TOKENIZER_HASH,
        "manifest_hash": "08da84cbd5cfe0ca9f9dec300589651e77d9ee36f8ee4c1b9047bc62c6f79fc3",
        "runtime_hash": RUNTIME_HASH,
        "device": "cpu",
        "torch_version": "2.12.1+cpu",
        "inference_code_version": "kronos-path-retention/1.0.0",
    }
    values.update(overrides)
    return KronosRuntimeIdentity.model_validate(values)


def sampling() -> KronosSamplingPolicy:
    return KronosSamplingPolicy(
        seed_policy="explicit-sequential-v1",
        seeds=(17, 18, 19),
        temperature=Decimal("1"),
        top_k=0,
        top_p=Decimal("0.9"),
    )


def series() -> BarSeries:
    bars = tuple(
        Bar(
            event_time=datetime(2026, 3, day, 21, tzinfo=UTC),
            published_at=datetime(2026, 3, day, 21, tzinfo=UTC),
            available_at=datetime(2026, 3, day, 21, tzinfo=UTC),
            observed_at=datetime(2026, 3, day, 21, tzinfo=UTC),
            open=Decimal("99"),
            high=Decimal("101"),
            low=Decimal("98"),
            close=Decimal("100"),
            volume=Decimal("1000"),
            amount=Decimal("100000"),
        )
        for day in (4, 5, 6)
    )
    return BarSeries(
        series_id=UUID("10000000-0000-4000-8000-000000000106"),
        instrument_id=INSTRUMENT_ID,
        interval="1d",
        adjustment="split_adjusted",
        session="regular",
        as_of=NOW,
        provider="golden",
        endpoint="fixture",
        raw_artifact_ref=f"sha256:{'c' * 64}",
        quality=DataQuality(
            status=DataQualityStatus.AVAILABLE,
            completeness=Decimal("1"),
        ),
        bars=bars,
    )


def calendar() -> ExchangeCalendar:
    return ExchangeCalendar(
        mic="XNYS",
        timezone="America/New_York",
        default=SessionTemplate(open_time=time(9, 30), close_time=time(16)),
        holidays=frozenset({date(2026, 3, 10)}),
    )


def worker_request(
    *,
    volume_quality: VolumeQuality = VolumeQuality.ESTIMATED,
    **overrides: object,
) -> KronosWorkerRequest:
    built = build_kronos_worker_request(
        request(),
        job_id=JOB_ID,
        attempt_generation=3,
        attempt_nonce="nonce-secret",
        mic="XNYS",
        series=series(),
        calendar=calendar(),
        runtime=runtime(),
        sampling=sampling(),
        volume_quality=volume_quality,
    )
    assert isinstance(built, Success)
    if not overrides:
        return built.value
    return KronosWorkerRequest.model_validate(
        built.value.model_dump(mode="json") | overrides
    )


def point(moment: datetime, close: str) -> KronosForecastPoint:
    value = Decimal(close)
    return KronosForecastPoint(
        timestamp=moment,
        open=value,
        high=value + 1,
        low=value - 1,
        close=value,
        volume=Decimal("1001"),
        amount=value * 1001,
    )


def worker_response(
    incoming: KronosWorkerRequest | None = None,
) -> KronosWorkerResponse:
    incoming = incoming or worker_request()
    closes = (("105", "110"), ("98", "90"), ("100", "100"))
    paths = tuple(
        KronosForecastPath(
            path_index=index,
            seed=seed,
            points=tuple(
                point(moment, close)
                for moment, close in zip(
                    incoming.future_timestamps, closes[index], strict=True
                )
            ),
        )
        for index, seed in enumerate(incoming.sampling.seeds)
    )
    qualities = {bar.volume_quality for bar in incoming.bars}
    input_quality = (
        VolumeQuality.MISSING
        if VolumeQuality.MISSING in qualities
        else (
            VolumeQuality.ESTIMATED
            if VolumeQuality.ESTIMATED in qualities
            else VolumeQuality.OBSERVED
        )
    )
    warning = {
        VolumeQuality.OBSERVED: (),
        VolumeQuality.ESTIMATED: ("input_volume_estimated",),
        VolumeQuality.MISSING: ("input_volume_missing",),
    }[input_quality]
    result = KronosWorkerResult(
        instrument_id=incoming.instrument_id,
        dataset_snapshot_id=incoming.dataset_snapshot_id,
        as_of=incoming.as_of,
        interval=incoming.interval,
        input_window_start=incoming.bars[0].event_time,
        input_window_end=incoming.bars[-1].event_time,
        future_timestamps=incoming.future_timestamps,
        input_last_close=incoming.bars[-1].close,
        input_volume_quality=input_quality,
        runtime=incoming.runtime,
        sampling=incoming.sampling,
        paths=paths,
        generated_at=NOW + timedelta(minutes=1),
        latency_ms=50,
        warnings=warning,
    )
    return KronosWorkerResponse(
        request_id=incoming.request_id,
        run_id=incoming.run_id,
        job_id=incoming.job_id,
        attempt_generation=incoming.attempt_generation,
        attempt_nonce=incoming.attempt_nonce,
        result_artifact_hash=result.payload_hash(),
        result=result,
    )


def envelope(response: KronosWorkerResponse) -> dict[str, object]:
    return {
        "success": True,
        "status": 200,
        "data": response.model_dump(mode="json"),
        "error": None,
        "metadata": None,
    }


def policy(**overrides: object) -> KronosHttpPolicy:
    values: dict[str, object] = {
        "policy_id": "kronos-cpu-v1",
        "profile": "cpu",
        "origin": "http://kronos-cpu:7200",
        "endpoint": "/v1/forecast",
        "timeout_seconds": 10,
        "max_request_bytes": 1_048_576,
        "max_response_bytes": 4_194_304,
        "max_transient_retries": 0,
        "max_absolute_step_return": "0.5",
    }
    values.update(overrides)
    return KronosHttpPolicy.model_validate(values)


class RecordingStore(MemoryArtifactStore):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def finalize(
        self, content: object, *, metadata: object, finalized_at: object
    ) -> object:
        self.sources.append(metadata.source)  # type: ignore[attr-defined]
        return super().finalize(content, metadata=metadata, finalized_at=finalized_at)


def subject(
    handler: object,
    *,
    store: RecordingStore | None = None,
    worker_policy: KronosHttpPolicy | None = None,
    clock: object = lambda: NOW + timedelta(minutes=2),
    credentials: RecordingServiceCredentialProvider | None = None,
) -> tuple[KronosHttpAdapter, httpx.Client, RecordingStore]:
    artifacts = store or RecordingStore()
    client = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    adapter = KronosHttpAdapter(
        client=client,
        artifacts=artifacts,
        policy=worker_policy or policy(),
        credentials=credentials or RecordingServiceCredentialProvider(),
        clock=clock,  # type: ignore[arg-type]
        monotonic_clock=lambda: 1.0,
    )
    return adapter, client, artifacts


def test_builder_uses_exchange_closes_across_weekend_holiday_and_dst() -> None:
    built = build_kronos_worker_request(
        request(),
        job_id=JOB_ID,
        attempt_generation=3,
        attempt_nonce="nonce-secret",
        mic="XNYS",
        series=series(),
        calendar=calendar(),
        runtime=runtime(),
        sampling=sampling(),
        volume_quality=VolumeQuality.ESTIMATED,
    )

    assert isinstance(built, Success)
    assert built.value.future_timestamps == (
        datetime(2026, 3, 9, 20, tzinfo=UTC),
        datetime(2026, 3, 11, 20, tzinfo=UTC),
    )
    assert all(
        bar.volume_quality is VolumeQuality.ESTIMATED for bar in built.value.bars
    )
    assert built.value.runtime.model_artifact_hash == request().model_artifact_hash

    missing = build_kronos_worker_request(
        request(),
        job_id=JOB_ID,
        attempt_generation=3,
        attempt_nonce="nonce-secret",
        mic="XNYS",
        series=series(),
        calendar=calendar(),
        runtime=runtime(),
        sampling=sampling(),
        volume_quality=VolumeQuality.MISSING,
    )
    assert isinstance(missing, Success)
    assert all(bar.volume is None and bar.amount is None for bar in missing.value.bars)


def test_pinned_cpu_and_cuda_core_configurations_are_closed() -> None:
    cpu = load_kronos_worker_configuration(
        ROOT / "config" / "workers" / "kronos_cpu.yaml"
    )
    cuda = load_kronos_worker_configuration(
        ROOT / "config" / "workers" / "kronos_cuda.yaml"
    )

    assert cpu.runtime.device == cpu.policy.profile == "cpu"
    assert cuda.runtime.device == cuda.policy.profile == "cuda"
    assert cpu.runtime.model_artifact_hash == cuda.runtime.model_artifact_hash
    assert cpu.runtime.runtime_hash != cuda.runtime.runtime_hash


def test_worker_configuration_rejects_origin_and_profile_drift(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        policy(origin="http://user:secret@kronos-cpu:7200")
    with pytest.raises(ValidationError, match="profiles must match"):
        type(
            load_kronos_worker_configuration(
                ROOT / "config" / "workers" / "kronos_cpu.yaml"
            )
        )(policy=policy(profile="cuda"), runtime=runtime())

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("unknown: true\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_kronos_worker_configuration(invalid)


def test_builder_fails_closed_on_series_calendar_or_runtime_drift() -> None:
    cases = (
        {"mic": "XNAS"},
        {"series": series().model_copy(update={"instrument_id": UUID(int=999)})},
        {"runtime": runtime(runtime_hash="2" * 64)},
    )
    for overrides in cases:
        built = build_kronos_worker_request(
            request(),
            job_id=JOB_ID,
            attempt_generation=3,
            attempt_nonce="nonce-secret",
            mic=overrides.get("mic", "XNYS"),  # type: ignore[arg-type]
            series=overrides.get("series", series()),  # type: ignore[arg-type]
            calendar=calendar(),
            runtime=overrides.get("runtime", runtime()),  # type: ignore[arg-type]
            sampling=sampling(),
            volume_quality=VolumeQuality.OBSERVED,
        )
        assert isinstance(built, Failure)
        assert built.error.code is ErrorCode.CONFLICT


@pytest.mark.parametrize(
    ("forecast", "bar_series"),
    [
        (request(interval="2d"), series().model_copy(update={"interval": "2d"})),
        (request(horizon_bars=257), series()),
    ],
)
def test_builder_rejects_unsupported_interval_and_horizon(
    forecast: ForecastRequest, bar_series: BarSeries
) -> None:
    result = build_kronos_worker_request(
        forecast,
        job_id=JOB_ID,
        attempt_generation=3,
        attempt_nonce="nonce-secret",
        mic="XNYS",
        series=bar_series,
        calendar=calendar(),
        runtime=runtime(),
        sampling=sampling(),
        volume_quality=VolumeQuality.OBSERVED,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INVALID_INPUT


def test_http_archives_raw_then_paths_before_mapping_signal() -> None:
    incoming = worker_request()

    def handler(sent: httpx.Request) -> httpx.Response:
        assert sent.url == "http://kronos-cpu:7200/v1/forecast"
        assert sent.headers["Accept-Encoding"] == "identity"
        assert sent.headers["Authorization"] == f"Bearer {TEST_SERVICE_TOKEN}"
        return httpx.Response(
            200, json=envelope(worker_response(incoming)), request=sent
        )

    adapter, client, artifacts = subject(handler)
    with client:
        output = adapter.forecast(incoming, request())

    assert isinstance(output, Success)
    assert artifacts.sources == [
        "kronos-isolated-worker-raw-response",
        "kronos-sample-paths",
    ]
    assert output.value.forecast.path_count == 3
    assert output.value.forecast.input_quality.status is DataQualityStatus.ESTIMATED
    assert output.value.forecast.model_revision == MODEL_REVISION
    assert artifacts.is_finalized(
        output.value.raw_output_artifact_ref.removeprefix("sha256:")
    )
    assert output.value.sampled_paths_artifact_ref is not None
    assert artifacts.is_finalized(
        output.value.sampled_paths_artifact_ref.removeprefix("sha256:")
    )


def test_http_fails_before_network_when_target_credential_is_unavailable() -> None:
    credentials = RecordingServiceCredentialProvider(available=False)
    adapter, client, _ = subject(
        lambda _request: pytest.fail("network must not be called"),
        credentials=credentials,
    )

    with client:
        result = adapter.forecast(worker_request(), request())

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.UNAUTHORIZED
    assert len(credentials.requests) == 1
    issued = credentials.requests[0]
    assert issued.receiver is ServiceReceiver.KRONOS
    assert issued.permission is Permission.DISPATCH_ASSIGNED_RESEARCH
    assert issued.target.kind is ResourceKind.JOB
    assert issued.target.identifier == str(JOB_ID)
    assert issued.attempt_generation == worker_request().attempt_generation
    assert issued.expires_no_later_than == worker_request().deadline


@pytest.mark.parametrize(
    ("quality", "expected"),
    [
        (VolumeQuality.OBSERVED, DataQualityStatus.AVAILABLE),
        (VolumeQuality.MISSING, DataQualityStatus.PARTIAL),
    ],
)
def test_volume_quality_deterministically_degrades_signal(
    quality: VolumeQuality, expected: DataQualityStatus
) -> None:
    incoming = worker_request(volume_quality=quality)
    adapter, client, _ = subject(
        lambda sent: httpx.Response(
            200, json=envelope(worker_response(incoming)), request=sent
        )
    )

    with client:
        output = adapter.forecast(incoming, request())

    assert isinstance(output, Success)
    assert output.value.forecast.input_quality.status is expected


def test_request_pair_mismatch_fails_before_network_or_artifacts() -> None:
    calls: list[int] = []
    adapter, client, artifacts = subject(
        lambda sent: (
            calls.append(1),
            httpx.Response(200, json=envelope(worker_response()), request=sent),
        )[1],
        worker_policy=policy(profile="cuda"),
    )

    with client:
        denied = adapter.forecast(worker_request(), request())

    assert isinstance(denied, Failure)
    assert denied.error.code is ErrorCode.CAPABILITY_DENIED
    assert calls == [] and artifacts.sources == []


@pytest.mark.parametrize(
    ("response", "expected", "archived"),
    [
        (
            lambda sent: httpx.Response(
                200,
                content=b"{}",
                headers={"content-type": "text/plain"},
                request=sent,
            ),
            ErrorCode.MODEL_OUTPUT_INVALID,
            False,
        ),
        (
            lambda sent: httpx.Response(413, request=sent),
            ErrorCode.PAYLOAD_TOO_LARGE,
            False,
        ),
        (
            lambda sent: httpx.Response(
                200,
                content=b"{}",
                headers={"content-type": "application/json"},
                request=sent,
            ),
            ErrorCode.MODEL_OUTPUT_INVALID,
            True,
        ),
    ],
)
def test_http_boundary_maps_media_status_and_invalid_envelope(
    response: object, expected: ErrorCode, archived: bool
) -> None:
    adapter, client, artifacts = subject(response)

    with client:
        output = adapter.forecast(worker_request(), request())

    assert isinstance(output, Failure)
    assert output.error.code is expected
    assert bool(artifacts.sources) is archived


def test_expired_request_and_oversized_response_fail_closed() -> None:
    calls: list[int] = []
    adapter, client, _ = subject(
        lambda sent: (
            calls.append(1),
            httpx.Response(200, content=b"{}", request=sent),
        )[1],
        clock=lambda: NOW + timedelta(minutes=6),
    )
    with client:
        expired = adapter.forecast(worker_request(), request())
    assert isinstance(expired, Failure)
    assert expired.error.code is ErrorCode.DEADLINE_EXCEEDED
    assert calls == []

    adapter, client, _ = subject(
        lambda sent: httpx.Response(
            200,
            content=b"{" + b"x" * 100 + b"}",
            headers={"content-type": "application/json"},
            request=sent,
        ),
        worker_policy=policy(max_response_bytes=100),
    )
    with client:
        oversized = adapter.forecast(worker_request(), request())
    assert isinstance(oversized, Failure)
    assert oversized.error.code is ErrorCode.PAYLOAD_TOO_LARGE


def test_extreme_model_path_fails_after_immutable_outputs_are_archived() -> None:
    incoming = worker_request()
    response = worker_response(incoming)
    extreme = response.result.paths[0].model_copy(
        update={
            "points": (
                point(incoming.future_timestamps[0], "200"),
                point(incoming.future_timestamps[1], "210"),
            )
        }
    )
    changed = response.result.model_copy(
        update={"paths": (extreme, *response.result.paths[1:])}
    )
    response = KronosWorkerResponse(
        request_id=response.request_id,
        run_id=response.run_id,
        job_id=response.job_id,
        attempt_generation=response.attempt_generation,
        attempt_nonce=response.attempt_nonce,
        result_artifact_hash=changed.payload_hash(),
        result=changed,
    )
    adapter, client, artifacts = subject(
        lambda sent: httpx.Response(200, json=envelope(response), request=sent)
    )

    with client:
        output = adapter.forecast(incoming, request())

    assert isinstance(output, Failure)
    assert output.error.code is ErrorCode.MODEL_OUTPUT_INVALID
    assert artifacts.sources == [
        "kronos-isolated-worker-raw-response",
        "kronos-sample-paths",
    ]


def test_lease_drift_fails_after_raw_and_path_artifacts_are_archived() -> None:
    incoming = worker_request()
    response = worker_response(incoming)
    payload = envelope(response)
    payload["data"]["attempt_nonce"] = "late"  # type: ignore[index]
    adapter, client, artifacts = subject(
        lambda sent: httpx.Response(200, json=payload, request=sent)
    )

    with client:
        output = adapter.forecast(incoming, request())

    assert isinstance(output, Failure)
    assert output.error.code is ErrorCode.CONFLICT
    assert artifacts.sources == [
        "kronos-isolated-worker-raw-response",
        "kronos-sample-paths",
    ]


def test_length_mismatch_archives_raw_but_never_produces_signal() -> None:
    incoming = worker_request()
    payload = envelope(worker_response(incoming))
    payload["data"]["result"]["paths"][0]["points"].pop()  # type: ignore[index]
    adapter, client, artifacts = subject(
        lambda sent: httpx.Response(200, json=payload, request=sent)
    )

    with client:
        output = adapter.forecast(incoming, request())

    assert isinstance(output, Failure)
    assert output.error.code is ErrorCode.MODEL_OUTPUT_INVALID
    assert artifacts.sources == ["kronos-isolated-worker-raw-response"]


def test_model_revision_mismatch_archives_paths_then_rejects_signal() -> None:
    incoming = worker_request()
    response = worker_response(incoming)
    drifted_runtime = response.result.runtime.model_copy(
        update={"model_revision": "f" * 40}
    )
    drifted_result = response.result.model_copy(update={"runtime": drifted_runtime})
    drifted = KronosWorkerResponse(
        request_id=response.request_id,
        run_id=response.run_id,
        job_id=response.job_id,
        attempt_generation=response.attempt_generation,
        attempt_nonce=response.attempt_nonce,
        result_artifact_hash=drifted_result.payload_hash(),
        result=drifted_result,
    )
    adapter, client, artifacts = subject(
        lambda sent: httpx.Response(200, json=envelope(drifted), request=sent)
    )

    with client:
        output = adapter.forecast(incoming, request())

    assert isinstance(output, Failure)
    assert output.error.code is ErrorCode.CONFLICT
    assert artifacts.sources == [
        "kronos-isolated-worker-raw-response",
        "kronos-sample-paths",
    ]


def test_replay_maps_from_sample_artifact_without_calling_worker() -> None:
    incoming = worker_request()
    adapter, client, artifacts = subject(
        lambda sent: httpx.Response(
            200, json=envelope(worker_response(incoming)), request=sent
        )
    )
    with client:
        first = adapter.forecast(incoming, request())
    assert isinstance(first, Success)
    assert first.value.sampled_paths_artifact_ref is not None

    replayed = replay_kronos_forecast(
        request(),
        raw_output_artifact_ref=first.value.raw_output_artifact_ref,
        sampled_paths_artifact_ref=first.value.sampled_paths_artifact_ref,
        artifacts=artifacts,
        policy=policy(),
    )

    assert isinstance(replayed, Success)
    assert replayed.value.forecast.payload_hash() == first.value.forecast.payload_hash()


def test_replay_rejects_missing_and_malformed_sample_artifacts() -> None:
    artifacts = MemoryArtifactStore()
    missing = replay_kronos_forecast(
        request(),
        raw_output_artifact_ref=f"sha256:{'d' * 64}",
        sampled_paths_artifact_ref=f"sha256:{'e' * 64}",
        artifacts=artifacts,
        policy=policy(),
    )
    assert isinstance(missing, Failure)
    assert missing.error.code is ErrorCode.NOT_FOUND

    stored = artifacts.finalize(
        b"{}",
        metadata=ArtifactMetadata(
            media_type="application/json",
            license_tag="LicenseRef-Kronos-Generated-Output",
            sensitivity=Sensitivity.INTERNAL,
            source="malformed-kronos-test",
        ),
        finalized_at=NOW,
    )
    assert isinstance(stored, Success)
    malformed = replay_kronos_forecast(
        request(),
        raw_output_artifact_ref=f"sha256:{'d' * 64}",
        sampled_paths_artifact_ref=f"sha256:{stored.value.content_hash}",
        artifacts=artifacts,
        policy=policy(),
    )

    assert isinstance(malformed, Failure)
    assert malformed.error.code is ErrorCode.CONFLICT


def test_archived_path_replay_matches_golden_signal() -> None:
    content = (GOLDEN / "sample_paths_v1.json").read_bytes()
    artifacts = MemoryArtifactStore()
    stored = artifacts.finalize(
        content,
        metadata=ArtifactMetadata(
            media_type="application/json",
            license_tag="LicenseRef-Kronos-Generated-Output",
            sensitivity=Sensitivity.INTERNAL,
            source="kronos-sample-paths-golden",
        ),
        finalized_at=NOW + timedelta(minutes=2),
    )
    assert isinstance(stored, Success)

    replayed = replay_kronos_forecast(
        request(),
        raw_output_artifact_ref=f"sha256:{'d' * 64}",
        sampled_paths_artifact_ref=f"sha256:{stored.value.content_hash}",
        artifacts=artifacts,
        policy=policy(),
    )
    expected = ForecastSignal.model_validate_json(
        (GOLDEN / "expected_signal_v1.json").read_bytes()
    )

    assert isinstance(replayed, Success)
    assert replayed.value.forecast.canonical_json() == expected.canonical_json()


def test_cpu_cuda_aggregate_golden_stays_within_declared_tolerance() -> None:
    fixture = json.loads(
        (GOLDEN / "profile_tolerance_v1.json").read_text(encoding="utf-8")
    )
    cpu = fixture["profiles"]["cpu"]
    cuda = fixture["profiles"]["cuda"]

    for metric, maximum in fixture["maximum_absolute_difference"].items():
        difference = abs(Decimal(cpu[metric]) - Decimal(cuda[metric]))
        assert difference <= Decimal(maximum), metric
