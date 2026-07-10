from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from stonks_agent.application.data.fetch_evidence import (
    FetchDataRequest,
    fetch_provider_data,
)
from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.provider_policy import (
    ProviderPolicy,
    ProviderRoute,
    load_provider_policies,
    reconcile_values,
)

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)


class FakeAdapter:
    def __init__(self, value: ProviderObservation[str]) -> None:
        self.value = value
        self.calls = 0

    def fetch(self, request: FetchDataRequest) -> ProviderObservation[str]:
        del request
        self.calls += 1
        return self.value


def observation(
    state: ProviderDataState,
    *,
    data: tuple[str, ...] = (),
    completeness: str = "0",
) -> ProviderObservation[str]:
    reasons = () if state in {
        ProviderDataState.AVAILABLE,
        ProviderDataState.LEGITIMATE_EMPTY,
    } else (state.value,)
    return ProviderObservation[str](
        state=state,
        data=data,
        completeness=Decimal(completeness),
        reasons=reasons,
        observed_at=NOW,
    )


def policy(*, allow_stale: bool = False, allow_partial: bool = False) -> ProviderPolicy:
    return ProviderPolicy(
        policy_id="us-prices/1",
        market="US",
        capability="prices",
        routes=(
            ProviderRoute(
                provider="primary",
                origin="https://primary.example.test",
                endpoints=("/prices",),
                freshness_seconds=60,
                quota_floor=1,
            ),
            ProviderRoute(
                provider="fallback",
                origin="https://fallback.example.test",
                endpoints=("/prices",),
                freshness_seconds=300,
                quota_floor=0,
            ),
        ),
        allow_stale=allow_stale,
        allow_partial=allow_partial,
        reconciliation_threshold=Decimal("0.01"),
    )


def test_default_policy_config_is_valid_and_ordered() -> None:
    policies = load_provider_policies(Path("config/providers/default.yaml"))

    assert {item.market for item in policies} == {"US", "HK", "TW"}
    us = next(item for item in policies if item.market == "US")
    assert tuple(route.provider for route in us.routes) == (
        "replay",
        "financial_datasets",
        "openbb_rest",
    )


@pytest.mark.parametrize(
    "route",
    [
        {
            "provider": "bad",
            "origin": "http://insecure.test",
            "endpoints": ("/prices",),
            "freshness_seconds": 1,
            "quota_floor": 0,
        },
        {
            "provider": "bad",
            "origin": "https://safe.test",
            "endpoints": ("https://attacker.test/data",),
            "freshness_seconds": 1,
            "quota_floor": 0,
        },
    ],
)
def test_policy_rejects_unsafe_origin_and_endpoint(route: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ProviderRoute.model_validate(route)


def test_missing_primary_adapter_falls_back_without_empty_success() -> None:
    fallback = FakeAdapter(
        observation(
            ProviderDataState.AVAILABLE,
            data=("bar",),
            completeness="1",
        )
    )
    result = fetch_provider_data(
        FetchDataRequest(
            market="US",
            capability="prices",
            as_of=NOW,
            query={"symbol": "AAPL"},
        ),
        policy=policy(),
        adapters={"fallback": fallback},
    )

    assert isinstance(result, Success)
    assert result.value.provider == "fallback"
    assert result.value.attempted_states == (
        ("primary", ProviderDataState.CONFIG_MISSING),
        ("fallback", ProviderDataState.AVAILABLE),
    )


@pytest.mark.parametrize(
    "state",
    [
        ProviderDataState.NOT_SUPPORTED,
        ProviderDataState.QUOTA_EXHAUSTED,
        ProviderDataState.CONFLICT,
        ProviderDataState.FETCH_FAILED,
    ],
)
def test_failed_provider_states_do_not_become_empty_success(
    state: ProviderDataState,
) -> None:
    adapters = {
        "primary": FakeAdapter(observation(state)),
        "fallback": FakeAdapter(observation(state)),
    }
    result = fetch_provider_data(
        FetchDataRequest(
            market="US",
            capability="prices",
            as_of=NOW,
            query={"symbol": "AAPL"},
        ),
        policy=policy(),
        adapters=adapters,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.DATA_UNAVAILABLE


def test_legitimate_empty_is_an_explicit_success() -> None:
    adapter = FakeAdapter(
        observation(ProviderDataState.LEGITIMATE_EMPTY, completeness="1")
    )
    result = fetch_provider_data(
        FetchDataRequest(
            market="US",
            capability="prices",
            as_of=NOW,
            query={"symbol": "AAPL"},
        ),
        policy=policy(),
        adapters={"primary": adapter},
    )

    assert isinstance(result, Success)
    assert result.value.observation.state is ProviderDataState.LEGITIMATE_EMPTY


def test_stale_data_requires_policy_opt_in() -> None:
    stale = FakeAdapter(
        observation(ProviderDataState.STALE, data=("bar",), completeness="1")
    )
    request = FetchDataRequest(
        market="US",
        capability="prices",
        as_of=NOW,
        query={"symbol": "AAPL"},
    )

    denied = fetch_provider_data(
        request,
        policy=policy(),
        adapters={"primary": stale},
    )
    accepted = fetch_provider_data(
        request,
        policy=policy(allow_stale=True),
        adapters={"primary": stale},
    )

    assert isinstance(denied, Failure)
    assert isinstance(accepted, Success)


def test_reconciliation_threshold_produces_conflict() -> None:
    accepted = reconcile_values(Decimal("100"), Decimal("100.5"), policy())
    conflict = reconcile_values(Decimal("100"), Decimal("102"), policy())

    assert accepted.state is ProviderDataState.AVAILABLE
    assert conflict.state is ProviderDataState.CONFLICT
