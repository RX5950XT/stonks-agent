"""Snapshot-bound bundle for market, company and filing evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from stonks_agent.adapters.market_data.official_instrument import (
    OfficialInstrumentDataSource,
)
from stonks_agent.adapters.market_data.openbb_rest import (
    OpenBBPrice,
    OpenBBRawFetch,
    OpenBBRestAdapter,
)
from stonks_agent.adapters.market_data.regional.base import RegionalProviderCapability
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.domain.clock import utc_now
from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_agent.domain.dataset_snapshot import (
    MaterializedEvidence,
    ProviderSnapshotMaterialization,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.evidence import AvailabilityCertainty, EvidenceTimeline
from stonks_agent.domain.instrument_data import InstrumentDataQuery, InstrumentOverview
from stonks_agent.ports.snapshot_materialization import SnapshotMaterializationSource
from stonks_contracts.common import canonical_json
from stonks_contracts.evidence import EvidenceKind, Sensitivity

RESEARCH_DATA_ENDPOINT = "/v1/instrument/research-data"
RESEARCH_POLICY_IDS = frozenset({"us-research/1", "tw-research/1"})
_PROVIDER = "stonks_bundle"
_VERSION = "stonks-research-bundle/1.0.0"
INSTRUMENT_BUNDLE_SUPPORT = frozenset(
    RegionalProviderCapability(
        provider=_PROVIDER,
        market=market,
        capability="research_data",
        endpoint=RESEARCH_DATA_ENDPOINT,
    )
    for market in ("US", "TW")
)


class InstrumentResearchSnapshotSource(SnapshotMaterializationSource[FetchDataRequest]):
    """Fetch all research inputs before the LLM receives a lease."""

    __slots__ = ("_clock", "_instrument", "_market")

    def __init__(
        self,
        *,
        market: OpenBBRestAdapter,
        instrument: OfficialInstrumentDataSource,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._market = market
        self._instrument = instrument
        self._clock = clock

    def fetch(
        self,
        request: FetchDataRequest,
        *,
        provider_policy_id: str,
    ) -> Result[ProviderSnapshotMaterialization]:
        expected_policy = f"{request.market.lower()}-research/1"
        if (
            request.capability != "research_data"
            or provider_policy_id not in RESEARCH_POLICY_IDS
            or provider_policy_id != expected_policy
        ):
            return _failure(
                ErrorCode.CAPABILITY_DENIED, "Research data policy is not allowlisted"
            )
        symbol = request.query.get("symbol")
        if not isinstance(symbol, str):
            return _failure(ErrorCode.INVALID_INPUT, "Research data symbol is invalid")
        observed_at = _aware(self._clock())
        if isinstance(observed_at, Failure):
            return observed_at
        if observed_at.value > request.as_of:
            return _failure(ErrorCode.CONFLICT, "Research data is newer than its as_of")
        instrument = self._instrument.fetch(
            InstrumentDataQuery(symbol=symbol, as_of=request.as_of),
            observed_at=observed_at.value,
        )
        if isinstance(instrument, Failure):
            return instrument
        prices = self._market.fetch_raw(
            FetchDataRequest(
                market=request.market,
                capability="prices",
                as_of=request.as_of,
                query=dict(request.query),
            )
        )
        if isinstance(prices, Failure):
            return prices
        return _materialize_bundle(request, prices.value, instrument.value)


def _materialize_bundle(
    request: FetchDataRequest,
    prices: OpenBBRawFetch,
    instrument: InstrumentOverview,
) -> Result[ProviderSnapshotMaterialization]:
    price_evidence = tuple(
        MaterializedEvidence(
            subject=f"instrument:{prices.symbol.lower()}",
            kind=EvidenceKind.MARKET_DATA,
            payload=_price_payload(item, prices.interval),
            timeline=item.bar.timeline,
        )
        for item in prices.observation.data
    )
    instrument_evidence = _instrument_evidence(instrument)
    evidence = (*price_evidence, *instrument_evidence)
    if not evidence:
        return _failure(
            ErrorCode.DATA_UNAVAILABLE, "Research bundle contains no evidence"
        )
    observation_at = max(
        prices.observation.observed_at,
        instrument.observed_at,
    )
    if observation_at > request.as_of:
        return _failure(ErrorCode.CONFLICT, "Research bundle observed after its as_of")
    payloads = tuple(item.payload for item in evidence)
    partial = instrument.state == "partial"
    raw_payload = _raw_bundle(prices.raw_payload, instrument)
    return Success(
        ProviderSnapshotMaterialization(
            provider=_PROVIDER,
            provider_version=_VERSION,
            endpoint=RESEARCH_DATA_ENDPOINT,
            raw_payload=raw_payload,
            raw_media_type="application/json",
            license_tag="mixed-public-provider-terms",
            redistribution_tag="internal-use-only",
            sensitivity=Sensitivity.INTERNAL,
            observation=ProviderObservation[object](
                state=ProviderDataState.PARTIAL
                if partial
                else ProviderDataState.AVAILABLE,
                data=payloads,
                completeness=Decimal("0.5") if partial else Decimal("1"),
                reasons=instrument.warnings if partial else (),
                observed_at=observation_at,
            ),
            evidence=evidence,
        )
    )


def _instrument_evidence(
    overview: InstrumentOverview,
) -> tuple[MaterializedEvidence, ...]:
    subject = f"instrument:{overview.symbol.lower()}"
    facts = tuple(
        MaterializedEvidence(
            subject=subject,
            kind=EvidenceKind.FUNDAMENTAL,
            payload={
                "data_type": "fundamental",
                **fact.model_dump(mode="json"),
            },
            timeline=_timeline(
                event_time=fact.event_time,
                published_at=fact.published_at,
                available_at=fact.available_at,
                observed_at=overview.observed_at,
                as_of=overview.as_of,
            ),
        )
        for fact in overview.facts
    )
    filings = tuple(
        MaterializedEvidence(
            subject=subject,
            kind=EvidenceKind.FILING,
            payload={
                "data_type": "filing",
                **filing.model_dump(mode="json"),
            },
            timeline=_timeline(
                event_time=filing.filed_at,
                published_at=filing.filed_at,
                available_at=filing.filed_at,
                observed_at=overview.observed_at,
                as_of=overview.as_of,
            ),
        )
        for filing in overview.filings
    )
    return (*facts, *filings)


def _timeline(
    *,
    event_time: datetime,
    published_at: datetime | None,
    available_at: datetime,
    observed_at: datetime,
    as_of: datetime,
) -> EvidenceTimeline:
    return EvidenceTimeline(
        event_time=event_time,
        published_at=published_at,
        available_at=available_at,
        observed_at=observed_at,
        as_of=as_of,
        availability_certainty=AvailabilityCertainty.PROVEN,
        strict_point_in_time=True,
    )


def _price_payload(price: OpenBBPrice, interval: str) -> dict[str, object]:
    bar = price.bar
    return {
        "data_type": "market_data",
        "event_time": bar.timeline.event_time.isoformat(),
        "interval": interval,
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": str(bar.volume),
    }


def _raw_bundle(raw_market: bytes, instrument: InstrumentOverview) -> bytes:
    try:
        market_payload = json.loads(raw_market)
    except (TypeError, ValueError):
        market_payload = {
            "sha256": hashlib.sha256(raw_market).hexdigest(),
            "raw_unparsed": True,
        }
    return canonical_json(
        {
            "bundle_version": _VERSION,
            "market_raw": market_payload,
            "instrument_projection": instrument.model_dump(mode="json"),
        }
    ).encode("utf-8")


def _aware(value: datetime) -> Result[datetime]:
    if value.tzinfo is None or value.utcoffset() is None:
        return _failure(
            ErrorCode.CONFIGURATION_INVALID, "Research data clock is invalid"
        )
    return Success(value.astimezone(UTC))


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
