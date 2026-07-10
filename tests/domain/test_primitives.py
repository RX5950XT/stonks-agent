from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from pydantic import ValidationError

from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.ids import EntityId, new_entity_id, parse_entity_id
from stonks_agent.domain.quality import QualityAssessment, QualityState
from stonks_agent.domain.time import normalize_utc


def test_entity_ids_are_namespaced_and_round_trip() -> None:
    generated = new_entity_id("run")
    parsed = parse_entity_id("run", str(generated.value))

    assert generated.namespace == "run"
    assert isinstance(parsed, Success)
    assert parsed.value == EntityId(namespace="run", value=generated.value)


def test_invalid_entity_id_returns_a_structured_failure() -> None:
    result = parse_entity_id("run", "not-a-uuid")

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INVALID_INPUT


def test_entity_id_rejects_unknown_external_fields() -> None:
    try:
        EntityId.model_validate(
            {
                "namespace": "run",
                "value": "00000000-0000-4000-8000-000000000000",
                "extra": True,
            }
        )
    except ValidationError as error:
        assert "extra" in str(error)
    else:  # pragma: no cover - documents a security invariant
        raise AssertionError("unknown input was accepted")


def test_timezone_aware_values_are_normalized_to_utc() -> None:
    local = datetime(2026, 7, 10, 9, 0, tzinfo=timezone(timedelta(hours=8)))
    result = normalize_utc(local)

    assert isinstance(result, Success)
    assert result.value == datetime(2026, 7, 10, 1, 0, tzinfo=UTC)


def test_naive_datetime_fails_closed() -> None:
    result = normalize_utc(datetime(2026, 7, 10, 9, 0))

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INVALID_INPUT


def test_quality_acceptance_is_explicit() -> None:
    valid = QualityAssessment(state=QualityState.VALID)
    stale = QualityAssessment(state=QualityState.STALE, reasons=("provider lag",))

    assert valid.is_acceptable({QualityState.VALID})
    assert not stale.is_acceptable({QualityState.VALID})
