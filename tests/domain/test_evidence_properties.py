from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_agent.domain.evidence import AvailabilityCertainty, EvidenceTimeline
from stonks_agent.domain.provenance import ProvenanceRecord

AS_OF = datetime(2026, 1, 2, 21, tzinfo=UTC)

PARTIAL_COMPLETENESS = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("0.99"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)
NON_PROVEN_CERTAINTY = st.sampled_from(
    [AvailabilityCertainty.ESTIMATED, AvailabilityCertainty.UNKNOWN]
)
FORBIDDEN_URL_CHARACTER = st.one_of(
    st.integers(min_value=0, max_value=31).map(chr),
    st.just(chr(127)),
    st.sampled_from([" ", "\u00a0", "\u2003"]),
)
SHA256 = st.binary(min_size=32, max_size=32).map(bytes.hex)


@given(completeness=PARTIAL_COMPLETENESS)
def test_stale_state_cannot_bypass_partial_acceptance(
    completeness: Decimal,
) -> None:
    with pytest.raises(ValidationError):
        ProviderObservation[str](
            state=ProviderDataState.STALE,
            data=("bar",),
            completeness=completeness,
            reasons=("freshness_exceeded",),
            observed_at=AS_OF,
        )


@given(
    state_and_completeness=st.sampled_from(
        [
            (ProviderDataState.STALE, Decimal("1")),
            (ProviderDataState.PARTIAL, Decimal("0.5")),
        ]
    )
)
def test_degraded_states_require_an_explicit_reason(
    state_and_completeness: tuple[ProviderDataState, Decimal],
) -> None:
    state, completeness = state_and_completeness

    with pytest.raises(ValidationError):
        ProviderObservation[str](
            state=state,
            data=("bar",),
            completeness=completeness,
            reasons=(),
            observed_at=AS_OF,
        )


@given(allow_stale=st.booleans(), allow_partial=st.booleans())
def test_degraded_acceptance_flags_remain_independent(
    allow_stale: bool,
    allow_partial: bool,
) -> None:
    stale = ProviderObservation[str](
        state=ProviderDataState.STALE,
        data=("bar",),
        completeness=Decimal("1"),
        reasons=("freshness_exceeded",),
        observed_at=AS_OF,
    )
    partial = ProviderObservation[str](
        state=ProviderDataState.PARTIAL,
        data=("bar",),
        completeness=Decimal("0.5"),
        reasons=("missing_bars",),
        observed_at=AS_OF,
    )

    assert (
        stale.accepted(
            allow_stale=allow_stale,
            allow_partial=allow_partial,
        )
        is allow_stale
    )
    assert (
        partial.accepted(
            allow_stale=allow_stale,
            allow_partial=allow_partial,
        )
        is allow_partial
    )


@given(
    certainty=NON_PROVEN_CERTAINTY,
    seconds_before_as_of=st.integers(min_value=0, max_value=86_400),
)
def test_strict_point_in_time_never_accepts_unproven_availability(
    certainty: AvailabilityCertainty,
    seconds_before_as_of: int,
) -> None:
    available_at = AS_OF - timedelta(seconds=seconds_before_as_of)

    with pytest.raises(ValidationError, match="proven availability"):
        EvidenceTimeline(
            event_time=available_at - timedelta(seconds=2),
            published_at=available_at - timedelta(seconds=1),
            available_at=available_at,
            observed_at=AS_OF,
            as_of=AS_OF,
            availability_certainty=certainty,
            strict_point_in_time=True,
        )


@given(character=FORBIDDEN_URL_CHARACTER)
def test_provenance_rejects_whitespace_and_controls_in_source_url(
    character: str,
) -> None:
    with pytest.raises(ValidationError):
        _provenance(
            source_url=f"https://api.example.test/prices{character}?symbol=AAPL"
        )


@given(raw_hash=SHA256, payload_hash=SHA256)
def test_provenance_round_trip_preserves_content_addresses(
    raw_hash: str,
    payload_hash: str,
) -> None:
    record = _provenance(
        raw_artifact_hash=raw_hash,
        payload_hash=payload_hash,
    )

    restored = ProvenanceRecord.model_validate(
        record.model_dump(mode="json", exclude_computed_fields=True)
    )

    assert restored == record
    assert restored.raw_artifact_ref == f"sha256:{raw_hash}"
    assert restored.payload_hash == payload_hash


def _provenance(**overrides: object) -> ProvenanceRecord:
    payload: dict[str, object] = {
        "provider": "financial_datasets",
        "provider_version": "2026-01",
        "endpoint": "/prices",
        "request_id": "request-123",
        "source_url": "https://api.example.test/prices?symbol=AAPL",
        "raw_artifact_hash": "a" * 64,
        "payload_hash": "b" * 64,
        "observed_at": AS_OF,
        "license_tag": "contract-only",
        "redistribution_tag": "none",
    }
    payload.update(overrides)
    return ProvenanceRecord.model_validate(payload)
