"""Policy-owned provider fallback without arbitrary provider or URL input."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.provider_policy import ProviderPolicy
from stonks_contracts.common import UTCDateTime


class FetchDataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    market: str = Field(pattern=r"^[A-Z0-9]{2,12}$")
    capability: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    as_of: UTCDateTime
    query: dict[str, object]


class ProviderAdapter(Protocol):
    def fetch(self, request: FetchDataRequest) -> object: ...


class FetchedProviderData[T](BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    observation: ProviderObservation[T]
    attempted_states: tuple[tuple[str, ProviderDataState], ...]


def fetch_provider_data[T](
    request: FetchDataRequest,
    *,
    policy: ProviderPolicy,
    adapters: Mapping[str, ProviderAdapter],
) -> Result[FetchedProviderData[T]]:
    if (request.market, request.capability) != (policy.market, policy.capability):
        return Failure(
            StructuredError(
                code=ErrorCode.INVALID_INPUT,
                message="Provider policy does not match the requested capability",
            )
        )
    attempted: list[tuple[str, ProviderDataState]] = []
    for route in policy.routes:
        adapter = adapters.get(route.provider)
        if adapter is None:
            attempted.append((route.provider, ProviderDataState.CONFIG_MISSING))
            continue
        try:
            raw_observation = adapter.fetch(request)
        except Exception:
            attempted.append((route.provider, ProviderDataState.FETCH_FAILED))
            continue
        if not isinstance(raw_observation, ProviderObservation):
            attempted.append((route.provider, ProviderDataState.FETCH_FAILED))
            continue
        observation = cast(ProviderObservation[T], raw_observation)
        attempted.append((route.provider, observation.state))
        if observation.accepted(
            allow_stale=policy.allow_stale,
            allow_partial=policy.allow_partial,
        ):
            return Success(
                FetchedProviderData[T](
                    provider=route.provider,
                    observation=observation,
                    attempted_states=tuple(attempted),
                )
            )
    return Failure(
        StructuredError(
            code=ErrorCode.DATA_UNAVAILABLE,
            message="No provider produced policy-acceptable data",
            details={
                "attempted_states": tuple(
                    (provider, state.value) for provider, state in attempted
                )
            },
        )
    )
