from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_contracts.kronos import (
    KronosBar,
    KronosForecastPath,
    KronosForecastPoint,
    KronosRuntimeIdentity,
    KronosSamplePathsArtifact,
    KronosSamplingPolicy,
    KronosWorkerRequest,
    KronosWorkerResponse,
    KronosWorkerResult,
    VolumeQuality,
)

NOW = datetime(2026, 1, 9, 21, tzinfo=UTC)
MODEL_REVISION = "901c26c1332695a2a8f243eb2f37243a37bea320"
TOKENIZER_REVISION = "0e0117387f39004a9016484a186a908917e22426"
MODEL_HASH = "b082dfcbd8e8c142a725c8bbb99781802f38fec81210e13479effb32b3c3e020"
TOKENIZER_HASH = "59d85f6af76a2c3b8240ea06cb21db4213b4eeca053f246b23e29cf832fc6bee"
RUNTIME_HASH = "1" * 64
MANIFEST_HASH = "08da84cbd5cfe0ca9f9dec300589651e77d9ee36f8ee4c1b9047bc62c6f79fc3"


def bar(day: int, **overrides: object) -> KronosBar:
    values: dict[str, object] = {
        "event_time": datetime(2026, 1, day, 21, tzinfo=UTC),
        "available_at": datetime(2026, 1, day, 21, tzinfo=UTC),
        "open": "100",
        "high": "102",
        "low": "99",
        "close": "101",
        "volume": "1000",
        "amount": "101000",
        "volume_quality": "observed",
    }
    values.update(overrides)
    return KronosBar.model_validate(values)


def runtime_identity(**overrides: object) -> KronosRuntimeIdentity:
    values: dict[str, object] = {
        "worker_version": "kronos-worker/0.2.0",
        "upstream_commit": "67b630e67f6a18c9e9be918d9b4337c960db1e9a",
        "model_id": "NeoQuasar/Kronos-small",
        "model_revision": MODEL_REVISION,
        "model_artifact_hash": MODEL_HASH,
        "tokenizer_id": "NeoQuasar/Kronos-Tokenizer-base",
        "tokenizer_revision": TOKENIZER_REVISION,
        "tokenizer_artifact_hash": TOKENIZER_HASH,
        "manifest_hash": MANIFEST_HASH,
        "runtime_hash": RUNTIME_HASH,
        "device": "cpu",
        "torch_version": "2.12.1+cpu",
        "inference_code_version": "kronos-path-retention/1.0.0",
    }
    values.update(overrides)
    return KronosRuntimeIdentity.model_validate(values)


def request(**overrides: object) -> KronosWorkerRequest:
    values: dict[str, object] = {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "run_id": "22222222-2222-4222-8222-222222222222",
        "job_id": "33333333-3333-4333-8333-333333333333",
        "attempt_generation": 2,
        "attempt_nonce": "nonce-2",
        "profile": "cpu",
        "instrument_id": "44444444-4444-4444-8444-444444444444",
        "mic": "XNAS",
        "dataset_snapshot_id": "55555555-5555-4555-8555-555555555555",
        "snapshot_artifact_ref": f"sha256:{'a' * 64}",
        "data_hash": "b" * 64,
        "as_of": NOW,
        "interval": "1d",
        "bars": (bar(7), bar(8), bar(9)),
        "future_timestamps": (
            datetime(2026, 1, 12, 21, tzinfo=UTC),
            datetime(2026, 1, 13, 21, tzinfo=UTC),
        ),
        "runtime": runtime_identity(),
        "sampling": {
            "seed_policy": "explicit-sequential-v1",
            "seeds": (20260109, 20260110, 20260111),
            "temperature": "1",
            "top_k": 0,
            "top_p": "0.9",
        },
        "deadline": NOW + timedelta(minutes=5),
    }
    values.update(overrides)
    return KronosWorkerRequest.model_validate(values)


def point(moment: datetime, close: str = "102") -> KronosForecastPoint:
    close_value = Decimal(close)
    return KronosForecastPoint(
        timestamp=moment,
        open=Decimal("101"),
        high=max(Decimal("103"), close_value),
        low=Decimal("100"),
        close=close_value,
        volume=Decimal("1001"),
        amount=Decimal("102102"),
    )


def result() -> KronosWorkerResult:
    incoming = request()
    paths = tuple(
        KronosForecastPath(
            path_index=index,
            seed=seed,
            points=tuple(point(moment, str(102 + index)) for moment in incoming.future_timestamps),
        )
        for index, seed in enumerate(incoming.sampling.seeds)
    )
    return KronosWorkerResult(
        instrument_id=incoming.instrument_id,
        dataset_snapshot_id=incoming.dataset_snapshot_id,
        as_of=incoming.as_of,
        interval=incoming.interval,
        input_window_start=incoming.bars[0].event_time,
        input_window_end=incoming.bars[-1].event_time,
        future_timestamps=incoming.future_timestamps,
        input_last_close=incoming.bars[-1].close,
        input_volume_quality=VolumeQuality.OBSERVED,
        runtime=incoming.runtime,
        sampling=incoming.sampling,
        paths=paths,
        generated_at=NOW + timedelta(seconds=1),
        latency_ms=25,
        warnings=(),
    )


def test_request_is_pit_ordered_calendar_bound_and_authority_free() -> None:
    value = request()

    assert value.horizon_bars == 2
    assert value.sampling.path_count == 3
    assert value.bars[-1].available_at <= value.as_of
    assert value.future_timestamps[0] > value.as_of
    with pytest.raises(ValidationError):
        KronosWorkerRequest.model_validate(value.model_dump(mode="json") | {"portfolio_target": {}})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bars", (bar(8), bar(7))),
        ("bars", (bar(7), bar(7))),
        ("bars", (bar(7), bar(8, available_at=NOW + timedelta(seconds=1)))),
        ("future_timestamps", (NOW, NOW + timedelta(days=1))),
        (
            "future_timestamps",
            (datetime(2026, 1, 12, 21, tzinfo=UTC),) * 2,
        ),
    ],
)
def test_request_rejects_order_time_and_pit_contamination(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        request(**{field: value})


def test_bar_volume_quality_and_ohlc_are_closed() -> None:
    missing = bar(7, volume=None, amount=None, volume_quality="missing")
    assert missing.volume is None

    for overrides in (
        {"volume": None, "volume_quality": "observed"},
        {"volume": "1", "volume_quality": "missing"},
        {"high": "98"},
        {"low": "101"},
        {"close": "0"},
    ):
        with pytest.raises(ValidationError):
            bar(7, **overrides)


def test_sampling_policy_requires_unique_explicit_seeds() -> None:
    with pytest.raises(ValidationError):
        KronosSamplingPolicy(
            seed_policy="explicit-sequential-v1",
            seeds=(7, 7),
            temperature=Decimal("1"),
            top_k=0,
            top_p=Decimal("0.9"),
        )


def test_worker_result_retains_all_paths_and_response_hash() -> None:
    incoming = request()
    output = result()
    response = KronosWorkerResponse(
        request_id=incoming.request_id,
        run_id=incoming.run_id,
        job_id=incoming.job_id,
        attempt_generation=incoming.attempt_generation,
        attempt_nonce=incoming.attempt_nonce,
        result_artifact_hash=output.payload_hash(),
        result=output,
    )

    assert tuple(path.seed for path in response.result.paths) == incoming.sampling.seeds
    assert all(len(path.points) == incoming.horizon_bars for path in output.paths)
    with pytest.raises(ValidationError):
        KronosWorkerResponse.model_validate(
            response.model_copy(update={"result_artifact_hash": "0" * 64}).model_dump(mode="json")
        )


def test_result_rejects_path_index_seed_length_and_timestamp_drift() -> None:
    output = result()
    first = output.paths[0]
    bad_paths = (
        first.model_copy(update={"seed": 999}),
        *output.paths[1:],
    )
    with pytest.raises(ValidationError):
        KronosWorkerResult.model_validate(
            output.model_dump(mode="json")
            | {"paths": [item.model_dump(mode="json") for item in bad_paths]}
        )

    bad_point = first.points[0].model_copy(update={"timestamp": NOW})
    bad_first = first.model_copy(update={"points": (bad_point, *first.points[1:])})
    with pytest.raises(ValidationError):
        KronosWorkerResult.model_validate(
            output.model_dump(mode="json")
            | {
                "paths": [
                    bad_first.model_dump(mode="json"),
                    *[item.model_dump(mode="json") for item in output.paths[1:]],
                ]
            }
        )


def test_sample_paths_artifact_is_replay_complete_without_lease_secret() -> None:
    output = result()
    artifact = KronosSamplePathsArtifact(
        request_id=UUID("11111111-1111-4111-8111-111111111111"),
        instrument_id=output.instrument_id,
        dataset_snapshot_id=output.dataset_snapshot_id,
        as_of=output.as_of,
        interval=output.interval,
        input_window_start=output.input_window_start,
        input_window_end=output.input_window_end,
        input_last_close=output.input_last_close,
        input_volume_quality=output.input_volume_quality,
        runtime=output.runtime,
        sampling=output.sampling,
        future_timestamps=output.future_timestamps,
        paths=output.paths,
        generated_at=output.generated_at,
        latency_ms=output.latency_ms,
        warnings=output.warnings,
    )

    serialized = artifact.canonical_json()
    assert "attempt_nonce" not in serialized
    assert len(artifact.paths) == 3
    assert (
        artifact.payload_hash()
        == KronosSamplePathsArtifact.model_validate_json(serialized).payload_hash()
    )
