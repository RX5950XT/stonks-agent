from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from stonks_agent.application.evaluation.quant_snapshot import (
    QuantInstrumentHistory,
    QuantSnapshotConversionRequest,
    convert_quant_snapshot,
)
from stonks_agent.domain.errors import Failure, Success
from stonks_contracts.market_data import (
    Bar,
    BarSeries,
    DataQuality,
    DataQualityStatus,
)
from stonks_contracts.quant_lab import (
    QuantFeatureSpec,
    QuantLabelSpec,
    QuantUniverseSpec,
)

NOW = datetime(2026, 7, 13, 5, tzinfo=UTC)
START = datetime(2025, 1, 2, 21, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
INSTRUMENTS = (
    UUID("20000000-0000-4000-8000-000000000911"),
    UUID("20000000-0000-4000-8000-000000000912"),
)


def _bar(index: int, instrument_index: int, **changes: object) -> Bar:
    event_at = START + timedelta(days=index)
    close = Decimal(100 + index + instrument_index)
    payload: dict[str, object] = {
        "event_time": event_at,
        "published_at": event_at,
        "available_at": event_at + timedelta(minutes=1),
        "observed_at": event_at + timedelta(minutes=2),
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": Decimal(1000 + index * 10 + instrument_index),
        "amount": close * Decimal(1000 + index * 10 + instrument_index),
    }
    return Bar.model_validate(payload | changes)


def _series(
    instrument_index: int,
    *,
    quality: DataQuality | None = None,
    bars: tuple[Bar, ...] | None = None,
) -> BarSeries:
    instrument_id = INSTRUMENTS[instrument_index]
    return BarSeries(
        series_id=uuid5(NAMESPACE_URL, f"quant-series:{instrument_index}"),
        instrument_id=instrument_id,
        interval="1d",
        adjustment="split_dividend_adjusted",
        session="regular",
        as_of=NOW,
        provider="canonical-replay",
        endpoint="snapshot",
        raw_artifact_ref=f"sha256:{HASH_A}",
        source_payload_hash=HASH_B,
        quality=quality
        or DataQuality(
            status=DataQualityStatus.AVAILABLE,
            completeness=Decimal(1),
        ),
        bars=bars or tuple(_bar(index, instrument_index) for index in range(12)),
    )


def _request(**changes: object) -> QuantSnapshotConversionRequest:
    universe = QuantUniverseSpec(
        instrument_ids=INSTRUMENTS,
        historical_membership_artifact_ref=f"sha256:{HASH_A}",
    )
    payload: dict[str, object] = {
        "dataset_snapshot_id": UUID("20000000-0000-4000-8000-000000000913"),
        "source_snapshot_artifact_ref": f"sha256:{HASH_A}",
        "source_data_hash": HASH_B,
        "as_of": NOW,
        "feature_spec": QuantFeatureSpec(
            names=("return_1", "return_5", "volatility_5", "volume_change_1"),
            lookback_bars=6,
        ),
        "label_spec": QuantLabelSpec(name="forward_return", horizon_bars=1),
        "universe_spec": universe,
        "histories": tuple(
            QuantInstrumentHistory(
                instrument_id=instrument_id,
                series=_series(index),
                historical_universe_known_at=START - timedelta(days=30),
                in_historical_universe=True,
            )
            for index, instrument_id in enumerate(INSTRUMENTS)
        ),
    }
    return QuantSnapshotConversionRequest.model_validate(payload | changes)


def test_converter_builds_stable_ordered_pit_rows_from_canonical_series() -> None:
    first = convert_quant_snapshot(_request())
    second = convert_quant_snapshot(_request())

    assert isinstance(first, Success)
    assert first == second
    artifact = first.value
    assert len(artifact.rows) == 12
    assert artifact.payload_hash() == second.value.payload_hash()
    assert all(value.label_available_at <= artifact.as_of for value in artifact.rows)
    assert tuple(
        (value.event_at, value.instrument_id.hex) for value in artifact.rows
    ) == tuple(
        sorted((value.event_at, value.instrument_id.hex) for value in artifact.rows)
    )
    assert artifact.rows[0].features[0] == Decimal(105) / Decimal(104) - 1
    assert artifact.rows[0].features[1] == Decimal(105) / Decimal(100) - 1


def test_converter_preserves_feature_order_and_forward_label() -> None:
    request = _request(
        feature_spec=QuantFeatureSpec(
            names=("volume_change_1", "return_1"), lookback_bars=2
        )
    )

    result = convert_quant_snapshot(request)

    assert isinstance(result, Success)
    first = result.value.rows[0]
    assert first.features == (Decimal("0.01"), Decimal(1) / Decimal(100))
    assert first.label == Decimal(102) / Decimal(101) - 1


def test_converter_fails_closed_for_quality_price_volume_or_universe_drift() -> None:
    stale = _series(
        0,
        quality=DataQuality(
            status=DataQualityStatus.STALE,
            completeness=Decimal(1),
        ),
    )
    stale_request = _request(
        histories=(
            _request().histories[0].model_copy(update={"series": stale}),
            _request().histories[1],
        )
    )
    zero_close_bars = list(_series(0).bars)
    zero_close_bars[5] = _bar(
        5,
        0,
        open=Decimal(0),
        high=Decimal(1),
        low=Decimal(0),
        close=Decimal(0),
    )
    bad_price = _request(
        histories=(
            _request()
            .histories[0]
            .model_copy(update={"series": _series(0, bars=tuple(zero_close_bars))}),
            _request().histories[1],
        )
    )
    zero_volume_bars = list(_series(0).bars)
    zero_volume_bars[4] = _bar(4, 0, volume=Decimal(0), amount=Decimal(0))
    bad_volume = _request(
        histories=(
            _request()
            .histories[0]
            .model_copy(update={"series": _series(0, bars=tuple(zero_volume_bars))}),
            _request().histories[1],
        )
    )
    wrong_universe = _request().model_copy(
        update={
            "histories": (
                _request()
                .histories[0]
                .model_copy(update={"in_historical_universe": False}),
                _request().histories[1],
            )
        }
    )

    for request in (stale_request, bad_price, bad_volume, wrong_universe):
        assert isinstance(convert_quant_snapshot(request), Failure)


def test_converter_rejects_source_snapshot_or_as_of_mismatch() -> None:
    wrong_as_of_series = _series(0).model_copy(
        update={"as_of": NOW - timedelta(days=1)}
    )
    wrong_as_of = _request(
        histories=(
            _request().histories[0].model_copy(update={"series": wrong_as_of_series}),
            _request().histories[1],
        )
    )
    duplicate = _request().model_copy(
        update={"histories": (_request().histories[0], _request().histories[0])}
    )

    assert isinstance(convert_quant_snapshot(wrong_as_of), Failure)
    assert isinstance(convert_quant_snapshot(duplicate), Failure)
