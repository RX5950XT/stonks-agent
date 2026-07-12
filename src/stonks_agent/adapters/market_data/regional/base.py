"""Validated regional mappings without ticker-suffix inference."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import ConfigDict, Field

from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_agent.domain.instrument import Instrument


class RegionalCapability(StrEnum):
    PRICES_DAILY = "prices_daily"
    PRICES_INTRADAY = "prices_intraday"
    CORPORATE_ACTIONS = "corporate_actions"
    FUNDAMENTALS = "fundamentals"


@dataclass(frozen=True)
class RegionalProviderCapability:
    """One exact market/capability/endpoint implemented by an adapter."""

    provider: str
    market: str
    capability: str
    endpoint: str


class RegionalInstrumentMapping(Instrument):
    model_config = ConfigDict(extra="forbid", frozen=True)

    supported_capabilities: frozenset[RegionalCapability] = Field(min_length=1)


class RegionalMarketDataAdapter(Protocol):
    @property
    def provider(self) -> str: ...

    @property
    def capabilities(self) -> frozenset[RegionalCapability]: ...

    def fetch(self, request: FetchDataRequest) -> ProviderObservation[object]: ...


def unsupported_observation[T](
    *,
    capability: RegionalCapability,
    observed_at: datetime,
) -> ProviderObservation[T]:
    return ProviderObservation[T](
        state=ProviderDataState.NOT_SUPPORTED,
        data=(),
        completeness=Decimal("0"),
        reasons=(f"unsupported:{capability.value}",),
        observed_at=observed_at,
    )


def load_regional_mappings(path: Path) -> tuple[RegionalInstrumentMapping, ...]:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError("regional mapping file could not be loaded") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("instruments"), list):
        raise ValueError("regional mapping file must contain an instruments list")
    mappings = tuple(
        RegionalInstrumentMapping.model_validate(item) for item in raw["instruments"]
    )
    identifiers = tuple(item.instrument_id for item in mappings)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("regional instrument IDs must be unique")
    return mappings
