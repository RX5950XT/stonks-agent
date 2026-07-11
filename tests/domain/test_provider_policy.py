from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from stonks_agent.application.data.fetch_evidence import (
    FetchDataRequest,
    fetch_provider_data,
)
from stonks_agent.domain.data_quality import (
    ProviderDataState,
    ProviderHealthState,
    ProviderObservation,
    ProviderRuntimeHealth,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.provider_policy import (
    ProviderPolicy,
    ProviderRoute,
    ReconciliationValue,
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


class RaisingAdapter:
    def fetch(self, request: FetchDataRequest) -> object:
        del request
        raise RuntimeError("provider_error=SENSITIVE_SENTINEL")


class DecimalStringStrategy:
    def extract(
        self,
        provider: str,
        observation: ProviderObservation[str],
    ) -> ReconciliationValue | None:
        del provider
        if not observation.data:
            return None
        return ReconciliationValue(metric="close", value=Decimal(observation.data[-1]))


class RaisingStrategy:
    def extract(
        self,
        provider: str,
        observation: ProviderObservation[str],
    ) -> ReconciliationValue | None:
        del provider, observation
        raise RuntimeError("strategy_error=SENSITIVE_SENTINEL")


def observation(
    state: ProviderDataState,
    *,
    data: tuple[str, ...] = (),
    completeness: str = "0",
) -> ProviderObservation[str]:
    reasons = (
        ()
        if state
        in {
            ProviderDataState.AVAILABLE,
            ProviderDataState.LEGITIMATE_EMPTY,
        }
        else (state.value,)
    )
    return ProviderObservation[str](
        state=state,
        data=data,
        completeness=Decimal(completeness),
        reasons=reasons,
        observed_at=NOW,
    )


def policy(
    *,
    allow_stale: bool = False,
    allow_partial: bool = False,
    primary_freshness_seconds: int = 0,
    primary_quota_floor: int = 0,
    fallback_freshness_seconds: int = 0,
    fallback_quota_floor: int = 0,
) -> ProviderPolicy:
    return ProviderPolicy(
        policy_id="us-prices/1",
        market="US",
        capability="prices",
        routes=(
            ProviderRoute(
                provider="primary",
                origin="https://primary.example.test",
                endpoints=("/prices",),
                freshness_seconds=primary_freshness_seconds,
                quota_floor=primary_quota_floor,
            ),
            ProviderRoute(
                provider="fallback",
                origin="https://fallback.example.test",
                endpoints=("/prices",),
                freshness_seconds=fallback_freshness_seconds,
                quota_floor=fallback_quota_floor,
            ),
        ),
        allow_stale=allow_stale,
        allow_partial=allow_partial,
        reconciliation_threshold=Decimal("0.01"),
    )


def runtime_health(
    *,
    latest_data_at: datetime | None = NOW,
    remaining_quota: int | None = 10,
    state: ProviderHealthState = ProviderHealthState.HEALTHY,
) -> ProviderRuntimeHealth:
    return ProviderRuntimeHealth(
        state=state,
        checked_at=NOW,
        latest_data_at=latest_data_at,
        remaining_quota=remaining_quota,
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
    assert {
        route.origin
        for item in policies
        for route in item.routes
        if route.provider == "openbb_rest"
    } == {"http://127.0.0.1:6900"}


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


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:6900",
        "http://[::1]:6900",
    ],
)
def test_policy_allows_plaintext_only_for_exact_loopback(origin: str) -> None:
    route = ProviderRoute(
        provider="loopback",
        origin=origin,
        endpoints=("/prices",),
        freshness_seconds=0,
        quota_floor=0,
    )

    assert route.origin == origin


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:6900",
        "http://127.0.0.2:6900",
        "http://127.1:6900",
        "http://2130706433:6900",
        "http://127.0.0.1.example.test:6900",
        "http://user:password@127.0.0.1:6900",
        "http://127.0.0.1:not-a-port",
    ],
)
def test_policy_rejects_non_exact_plaintext_loopback(origin: str) -> None:
    with pytest.raises(ValidationError):
        ProviderRoute(
            provider="loopback",
            origin=origin,
            endpoints=("/prices",),
            freshness_seconds=0,
            quota_floor=0,
        )


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


def test_unknown_required_freshness_fails_closed_and_uses_fallback() -> None:
    primary = FakeAdapter(
        observation(ProviderDataState.AVAILABLE, data=("100",), completeness="1")
    )
    fallback = FakeAdapter(
        observation(ProviderDataState.AVAILABLE, data=("101",), completeness="1")
    )

    result = fetch_provider_data(
        FetchDataRequest(
            market="US",
            capability="prices",
            as_of=NOW,
            query={"symbol": "AAPL"},
        ),
        policy=policy(primary_freshness_seconds=60),
        adapters={"primary": primary, "fallback": fallback},
        runtime_health={
            "primary": runtime_health(latest_data_at=None),
        },
    )

    assert isinstance(result, Success)
    assert result.value.provider == "fallback"
    assert result.value.attempted_states == (
        ("primary", ProviderDataState.FRESHNESS_UNKNOWN),
        ("fallback", ProviderDataState.AVAILABLE),
    )
    assert primary.calls == 0


def test_stale_runtime_data_obeys_route_limit_and_policy_opt_in() -> None:
    primary = FakeAdapter(
        observation(ProviderDataState.AVAILABLE, data=("100",), completeness="1")
    )
    request = FetchDataRequest(
        market="US",
        capability="prices",
        as_of=NOW,
        query={"symbol": "AAPL"},
    )
    stale_health = {
        "primary": runtime_health(
            latest_data_at=datetime(2026, 1, 2, 20, 58, tzinfo=UTC)
        )
    }

    denied = fetch_provider_data(
        request,
        policy=policy(primary_freshness_seconds=60),
        adapters={"primary": primary},
        runtime_health=stale_health,
    )
    accepted = fetch_provider_data(
        request,
        policy=policy(allow_stale=True, primary_freshness_seconds=60),
        adapters={"primary": primary},
        runtime_health=stale_health,
    )

    assert isinstance(denied, Failure)
    assert denied.error.details["attempted_states"] == (
        ("primary", "stale"),
        ("fallback", "config_missing"),
    )
    assert isinstance(accepted, Success)
    assert accepted.value.observation.state is ProviderDataState.STALE


def test_unknown_or_exhausted_quota_fails_closed_before_fetch() -> None:
    primary = FakeAdapter(
        observation(ProviderDataState.AVAILABLE, data=("100",), completeness="1")
    )
    fallback = FakeAdapter(
        observation(ProviderDataState.AVAILABLE, data=("101",), completeness="1")
    )
    request = FetchDataRequest(
        market="US",
        capability="prices",
        as_of=NOW,
        query={"symbol": "AAPL"},
    )

    unknown = fetch_provider_data(
        request,
        policy=policy(primary_quota_floor=1),
        adapters={"primary": primary, "fallback": fallback},
        runtime_health={"primary": runtime_health(remaining_quota=None)},
    )
    exhausted = fetch_provider_data(
        request,
        policy=policy(primary_quota_floor=1),
        adapters={"primary": primary, "fallback": fallback},
        runtime_health={"primary": runtime_health(remaining_quota=0)},
    )

    assert isinstance(unknown, Success)
    assert unknown.value.attempted_states[0] == (
        "primary",
        ProviderDataState.QUOTA_UNKNOWN,
    )
    assert isinstance(exhausted, Success)
    assert exhausted.value.attempted_states[0] == (
        "primary",
        ProviderDataState.QUOTA_EXHAUSTED,
    )
    assert primary.calls == 0
    assert fallback.calls == 2


def test_unhealthy_provider_is_skipped_without_calling_adapter() -> None:
    primary = FakeAdapter(
        observation(ProviderDataState.AVAILABLE, data=("100",), completeness="1")
    )
    fallback = FakeAdapter(
        observation(ProviderDataState.AVAILABLE, data=("101",), completeness="1")
    )

    result = fetch_provider_data(
        FetchDataRequest(
            market="US",
            capability="prices",
            as_of=NOW,
            query={"symbol": "AAPL"},
        ),
        policy=policy(),
        adapters={"primary": primary, "fallback": fallback},
        runtime_health={
            "primary": runtime_health(state=ProviderHealthState.UNAVAILABLE)
        },
    )

    assert isinstance(result, Success)
    assert result.value.attempted_states[0] == (
        "primary",
        ProviderDataState.PROVIDER_UNHEALTHY,
    )
    assert primary.calls == 0


def test_unknown_provider_health_fails_closed_and_uses_fallback() -> None:
    primary = FakeAdapter(
        observation(ProviderDataState.AVAILABLE, data=("100",), completeness="1")
    )
    fallback = FakeAdapter(
        observation(ProviderDataState.AVAILABLE, data=("101",), completeness="1")
    )

    result = fetch_provider_data(
        FetchDataRequest(
            market="US",
            capability="prices",
            as_of=NOW,
            query={"symbol": "AAPL"},
        ),
        policy=policy(),
        adapters={"primary": primary, "fallback": fallback},
        runtime_health={
            "primary": runtime_health(state=ProviderHealthState.UNKNOWN)
        },
    )

    assert isinstance(result, Success)
    assert result.value.attempted_states[0] == (
        "primary",
        ProviderDataState.HEALTH_UNKNOWN,
    )
    assert primary.calls == 0
    assert fallback.calls == 1


def test_reconciliation_strategy_accepts_values_within_threshold() -> None:
    primary = FakeAdapter(
        observation(ProviderDataState.AVAILABLE, data=("100",), completeness="1")
    )
    fallback = FakeAdapter(
        observation(ProviderDataState.AVAILABLE, data=("100.5",), completeness="1")
    )

    result = fetch_provider_data(
        FetchDataRequest(
            market="US",
            capability="prices",
            as_of=NOW,
            query={"symbol": "AAPL"},
        ),
        policy=policy(),
        adapters={"primary": primary, "fallback": fallback},
        reconciliation_strategy=DecimalStringStrategy(),
    )

    assert isinstance(result, Success)
    assert result.value.provider == "primary"
    assert primary.calls == fallback.calls == 1


def test_reconciliation_conflict_returns_data_unavailable_not_empty_success() -> None:
    primary = FakeAdapter(
        observation(ProviderDataState.AVAILABLE, data=("100",), completeness="1")
    )
    fallback = FakeAdapter(
        observation(ProviderDataState.AVAILABLE, data=("102",), completeness="1")
    )

    result = fetch_provider_data(
        FetchDataRequest(
            market="US",
            capability="prices",
            as_of=NOW,
            query={"symbol": "AAPL"},
        ),
        policy=policy(),
        adapters={"primary": primary, "fallback": fallback},
        reconciliation_strategy=DecimalStringStrategy(),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.DATA_UNAVAILABLE
    assert result.error.details["reconciliation_state"] == "conflict"
    assert result.error.details["providers"] == ("primary", "fallback")


def test_reconciliation_rejects_empty_and_available_disagreement() -> None:
    primary = FakeAdapter(
        observation(ProviderDataState.LEGITIMATE_EMPTY, completeness="1")
    )
    fallback = FakeAdapter(
        observation(ProviderDataState.AVAILABLE, data=("100",), completeness="1")
    )

    result = fetch_provider_data(
        FetchDataRequest(
            market="US",
            capability="prices",
            as_of=NOW,
            query={"symbol": "AAPL"},
        ),
        policy=policy(),
        adapters={"primary": primary, "fallback": fallback},
        reconciliation_strategy=DecimalStringStrategy(),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.DATA_UNAVAILABLE
    assert result.error.details["reconciliation_state"] == "conflict"
    assert result.error.details["reason"] == "reconciliation_empty_mismatch"


def test_two_legitimate_empty_observations_reconcile_as_explicit_success() -> None:
    empty = observation(ProviderDataState.LEGITIMATE_EMPTY, completeness="1")

    result = fetch_provider_data(
        FetchDataRequest(
            market="US",
            capability="prices",
            as_of=NOW,
            query={"symbol": "AAPL"},
        ),
        policy=policy(),
        adapters={"primary": FakeAdapter(empty), "fallback": FakeAdapter(empty)},
        reconciliation_strategy=DecimalStringStrategy(),
    )

    assert isinstance(result, Success)
    assert result.value.observation.state is ProviderDataState.LEGITIMATE_EMPTY


def test_provider_and_reconciliation_errors_do_not_leak_secrets() -> None:
    available = FakeAdapter(
        observation(ProviderDataState.AVAILABLE, data=("100",), completeness="1")
    )
    request = FetchDataRequest(
        market="US",
        capability="prices",
        as_of=NOW,
        query={"symbol": "AAPL"},
    )

    with capture_logs() as logs:
        provider_failure = fetch_provider_data(
            request,
            policy=policy(),
            adapters={"primary": RaisingAdapter()},
        )
        strategy_failure = fetch_provider_data(
            request,
            policy=policy(),
            adapters={"primary": available, "fallback": available},
            reconciliation_strategy=RaisingStrategy(),
        )

    assert isinstance(provider_failure, Failure)
    assert isinstance(strategy_failure, Failure)
    assert "SENSITIVE_SENTINEL" not in repr(provider_failure)
    assert "SENSITIVE_SENTINEL" not in repr(strategy_failure)
    assert strategy_failure.error.details["reconciliation_state"] == "conflict"
    assert [
        {
            "provider": entry["provider"],
            "stage": entry["stage"],
            "error_type": entry["error_type"],
        }
        for entry in logs
    ] == [
        {
            "provider": "primary",
            "stage": "fetch",
            "error_type": "RuntimeError",
        },
        {
            "provider": "primary",
            "stage": "reconciliation_extract",
            "error_type": "RuntimeError",
        },
    ]
    assert "SENSITIVE_SENTINEL" not in repr(logs)
