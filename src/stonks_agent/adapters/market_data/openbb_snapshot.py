"""Canonical snapshot materialization from one actual OpenBB REST response."""

from __future__ import annotations

from stonks_agent.adapters.market_data.openbb_rest import (
    OPENBB_HISTORICAL_ENDPOINT,
    OpenBBPrice,
    OpenBBRestAdapter,
)
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
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
from stonks_contracts.evidence import EvidenceKind, Sensitivity

_POLICY_ID = "us-prices/1"
_PROVIDER_VERSION = "openbb-rest/1.0.0"


class OpenBBSnapshotMaterializationSource:
    """Convert validated OpenBB bars without reconstructing provider bytes."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: OpenBBRestAdapter) -> None:
        self._adapter = adapter

    def fetch(
        self,
        request: FetchDataRequest,
        *,
        provider_policy_id: str,
    ) -> Result[ProviderSnapshotMaterialization]:
        if provider_policy_id != _POLICY_ID:
            return _failure(
                ErrorCode.CAPABILITY_DENIED,
                "OpenBB snapshot policy is not allowlisted",
            )
        fetched = self._adapter.fetch_raw(request)
        if isinstance(fetched, Failure):
            return fetched
        raw = fetched.value
        if (
            raw.observation.state is not ProviderDataState.AVAILABLE
            or not raw.observation.data
        ):
            return _failure(
                ErrorCode.DATA_UNAVAILABLE,
                "OpenBB returned no snapshot evidence",
            )
        payloads = tuple(
            _payload(price, raw.interval) for price in raw.observation.data
        )
        evidence = tuple(
            MaterializedEvidence(
                subject=f"instrument:{raw.symbol.lower()}",
                kind=EvidenceKind.MARKET_DATA,
                payload=payload,
                timeline=price.bar.timeline,
            )
            for price, payload in zip(
                raw.observation.data,
                payloads,
                strict=True,
            )
        )
        return Success(
            ProviderSnapshotMaterialization(
                provider="openbb_rest",
                provider_version=_PROVIDER_VERSION,
                endpoint=OPENBB_HISTORICAL_ENDPOINT,
                raw_payload=raw.raw_payload,
                raw_media_type="application/json",
                license_tag="provider-terms",
                redistribution_tag="internal-use-only",
                sensitivity=Sensitivity.INTERNAL,
                observation=ProviderObservation[object](
                    state=raw.observation.state,
                    data=payloads,
                    completeness=raw.observation.completeness,
                    reasons=raw.observation.reasons,
                    observed_at=raw.observation.observed_at,
                ),
                evidence=evidence,
            )
        )


def _payload(price: OpenBBPrice, interval: str) -> dict[str, object]:
    bar = price.bar
    return {
        "event_time": bar.timeline.event_time.isoformat(),
        "interval": interval,
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": str(bar.volume),
    }


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
