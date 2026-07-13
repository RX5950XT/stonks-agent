"""Point-in-time Post-Earnings Announcement Drift research strategy.

Derived from the PEAD data-cleaning approach in virattt/ai-hedge-fund at
commit 3a18702cb25777fb4bdb4b2527a0c868bc8297f4 (MIT). This implementation
uses Stonks Agent contracts and never creates portfolio targets or orders.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, model_validator

from stonks_contracts.common import UTCDateTime, stable_payload_hash
from stonks_contracts.signal import (
    AlphaSignal,
    PromotionState,
    SignalDirection,
)

STRATEGY_ID = "pead"
STRATEGY_VERSION = "1.0.0"
_RETROSPECTIVE_CUTOFF_DAYS = 45
_SIGNAL_WINDOW_DAYS = 4


class FilingSource(StrEnum):
    EIGHT_K = "8-K"
    TEN_Q = "10-Q"
    TEN_K = "10-K"
    TWENTY_F = "20-F"
    OTHER = "other"


class EarningsSurprise(StrEnum):
    BEAT = "BEAT"
    MISS = "MISS"
    MEET = "MEET"


class EarningsEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    evidence_id: UUID
    instrument_id: UUID
    report_period: date
    filing_at: UTCDateTime
    available_at: UTCDateTime
    source_type: FilingSource
    surprise: EarningsSurprise
    quarterly: Literal[True]
    availability_certainty: Literal["proven", "unknown"]

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        if self.filing_at.date() < self.report_period:
            raise ValueError("filing cannot precede report period")
        if self.available_at < self.filing_at:
            raise ValueError("availability cannot precede filing")
        return self


class PEADStrategy:
    """Produce draft-only alpha views from proven, fresh earnings evidence."""

    def evaluate(
        self,
        instrument_id: UUID,
        as_of: datetime,
        events: tuple[EarningsEvent, ...],
    ) -> AlphaSignal:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        normalized_as_of = as_of.astimezone(UTC)
        selected = _select_event(instrument_id, normalized_as_of, events)
        if selected is None:
            return _neutral_signal(instrument_id, normalized_as_of)
        direction = (
            SignalDirection.LONG
            if selected.surprise is EarningsSurprise.BEAT
            else SignalDirection.SHORT
        )
        value = Decimal(1) if direction is SignalDirection.LONG else Decimal(-1)
        expires_at = selected.available_at + timedelta(
            days=_SIGNAL_WINDOW_DAYS, microseconds=1
        )
        return AlphaSignal(
            signal_id=_signal_id(instrument_id, normalized_as_of, selected),
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            instrument_id=instrument_id,
            as_of=normalized_as_of,
            horizon=f"{_SIGNAL_WINDOW_DAYS} calendar days",
            value=value,
            confidence=Decimal(0),
            expires_at=expires_at,
            direction=direction,
            evidence_refs=(selected.evidence_id,),
            reason_codes=(
                f"eps_surprise:{selected.surprise.value}",
                f"source:{selected.source_type.value}",
                f"report_period:{selected.report_period.isoformat()}",
                "research_only_unevaluated",
            ),
            promotion_state=PromotionState.DRAFT,
        )


def _select_event(
    instrument_id: UUID,
    as_of: datetime,
    events: tuple[EarningsEvent, ...],
) -> EarningsEvent | None:
    eligible = tuple(
        event
        for event in events
        if event.instrument_id == instrument_id
        and event.availability_certainty == "proven"
        and event.available_at <= as_of
        and event.surprise in {EarningsSurprise.BEAT, EarningsSurprise.MISS}
        and 0
        <= (event.filing_at.date() - event.report_period).days
        < _RETROSPECTIVE_CUTOFF_DAYS
    )
    deduplicated: dict[date, EarningsEvent] = {}
    for event in eligible:
        current = deduplicated.get(event.report_period)
        if current is None or _event_priority(event) < _event_priority(current):
            deduplicated[event.report_period] = event
    fresh = tuple(
        event
        for event in deduplicated.values()
        if timedelta(0)
        <= as_of - event.available_at
        <= timedelta(days=_SIGNAL_WINDOW_DAYS)
    )
    return max(fresh, key=_event_recency) if fresh else None


def _event_priority(event: EarningsEvent) -> tuple[int, datetime, str]:
    priorities = {
        FilingSource.EIGHT_K: 0,
        FilingSource.TEN_Q: 1,
        FilingSource.TEN_K: 2,
        FilingSource.TWENTY_F: 3,
        FilingSource.OTHER: 99,
    }
    return priorities[event.source_type], event.available_at, str(event.event_id)


def _event_recency(event: EarningsEvent) -> tuple[datetime, str]:
    return event.available_at, str(event.event_id)


def _neutral_signal(instrument_id: UUID, as_of: datetime) -> AlphaSignal:
    payload = {
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "instrument_id": str(instrument_id),
        "as_of": as_of.isoformat(),
        "state": "neutral",
    }
    return AlphaSignal(
        signal_id=uuid5(NAMESPACE_URL, stable_payload_hash(payload)),
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        instrument_id=instrument_id,
        as_of=as_of,
        horizon=f"{_SIGNAL_WINDOW_DAYS} calendar days",
        value=Decimal(0),
        confidence=Decimal(0),
        expires_at=as_of + timedelta(days=1),
        direction=SignalDirection.NEUTRAL,
        reason_codes=("no_qualifying_earnings_event", "research_only_unevaluated"),
        promotion_state=PromotionState.DRAFT,
    )


def _signal_id(
    instrument_id: UUID,
    as_of: datetime,
    event: EarningsEvent,
) -> UUID:
    payload = {
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "instrument_id": str(instrument_id),
        "as_of": as_of.isoformat(),
        "event_id": str(event.event_id),
        "event_hash": stable_payload_hash(event.model_dump(mode="json")),
    }
    return uuid5(NAMESPACE_URL, stable_payload_hash(payload))
