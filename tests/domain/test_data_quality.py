from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)


@pytest.mark.parametrize(
    "state",
    [
        ProviderDataState.NOT_SUPPORTED,
        ProviderDataState.CONFIG_MISSING,
        ProviderDataState.QUOTA_EXHAUSTED,
        ProviderDataState.CONFLICT,
        ProviderDataState.FETCH_FAILED,
    ],
)
def test_failure_states_are_distinct_and_have_no_canonical_data(
    state: ProviderDataState,
) -> None:
    observation = ProviderObservation[str](
        state=state,
        data=(),
        completeness=Decimal("0"),
        reasons=(state.value,),
        observed_at=NOW,
    )

    assert observation.state is state
    assert observation.data == ()
    assert observation.is_usable is False


def test_legitimate_empty_is_not_fetch_failure() -> None:
    empty = ProviderObservation[str](
        state=ProviderDataState.LEGITIMATE_EMPTY,
        data=(),
        completeness=Decimal("1"),
        observed_at=NOW,
    )
    failed = ProviderObservation[str](
        state=ProviderDataState.FETCH_FAILED,
        data=(),
        completeness=Decimal("0"),
        reasons=("timeout",),
        observed_at=NOW,
    )

    assert empty.is_usable is True
    assert failed.is_usable is False
    assert empty.state is not failed.state


@pytest.mark.parametrize(
    "payload",
    [
        {
            "state": ProviderDataState.AVAILABLE,
            "data": (),
            "completeness": "1",
        },
        {
            "state": ProviderDataState.PARTIAL,
            "data": ("bar",),
            "completeness": "1",
        },
        {
            "state": ProviderDataState.LEGITIMATE_EMPTY,
            "data": ("unexpected",),
            "completeness": "1",
        },
        {
            "state": ProviderDataState.FETCH_FAILED,
            "data": ("unexpected",),
            "completeness": "0",
        },
    ],
)
def test_invalid_state_payload_combinations_fail_closed(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ProviderObservation[str].model_validate(payload | {"observed_at": NOW})


def test_stale_and_partial_data_require_explicit_acceptance() -> None:
    stale = ProviderObservation[str](
        state=ProviderDataState.STALE,
        data=("bar",),
        completeness=Decimal("1"),
        reasons=("freshness_exceeded",),
        observed_at=NOW,
    )
    partial = ProviderObservation[str](
        state=ProviderDataState.PARTIAL,
        data=("bar",),
        completeness=Decimal("0.5"),
        reasons=("one_market_missing",),
        observed_at=NOW,
    )

    assert stale.is_usable is False
    assert stale.accepted(allow_stale=True, allow_partial=False)
    assert partial.is_usable is False
    assert partial.accepted(allow_stale=False, allow_partial=True)
