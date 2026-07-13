from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from stonks_agent.strategies.pead import (
    EarningsEvent,
    EarningsSurprise,
    FilingSource,
    PEADStrategy,
)
from stonks_contracts.signal import PromotionState, SignalDirection

GOLDEN = Path("tests/golden/pead/beat_signal.json")
AS_OF = datetime(2026, 5, 3, 14, tzinfo=UTC)
INSTRUMENT_ID = UUID("30000000-0000-4000-8000-000000000001")
EVIDENCE_ID = UUID("30000000-0000-4000-8000-000000000002")


def event(**overrides: object) -> EarningsEvent:
    values: dict[str, object] = {
        "event_id": UUID("30000000-0000-4000-8000-000000000003"),
        "evidence_id": EVIDENCE_ID,
        "instrument_id": INSTRUMENT_ID,
        "report_period": date(2026, 3, 31),
        "filing_at": datetime(2026, 5, 1, 12, tzinfo=UTC),
        "available_at": datetime(2026, 5, 1, 12, 1, tzinfo=UTC),
        "source_type": FilingSource.EIGHT_K,
        "surprise": EarningsSurprise.BEAT,
        "quarterly": True,
        "availability_certainty": "proven",
    }
    values.update(overrides)
    return EarningsEvent.model_validate(values)


def test_beat_signal_matches_golden_and_remains_draft() -> None:
    signal = PEADStrategy().evaluate(INSTRUMENT_ID, AS_OF, (event(),))
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))

    assert signal.model_dump(mode="json") == expected
    assert signal.direction is SignalDirection.LONG
    assert signal.promotion_state is PromotionState.DRAFT
    assert signal.confidence == 0
    assert signal.evidence_refs == (EVIDENCE_ID,)


def test_future_unknown_stale_and_retrospective_events_are_neutral() -> None:
    cases = (
        event(
            event_id=UUID(int=10),
            available_at=AS_OF + timedelta(seconds=1),
            filing_at=AS_OF + timedelta(seconds=1),
        ),
        event(event_id=UUID(int=11), availability_certainty="unknown"),
        event(
            event_id=UUID(int=12),
            filing_at=AS_OF - timedelta(days=10),
            available_at=AS_OF - timedelta(days=10),
        ),
        event(
            event_id=UUID(int=13),
            report_period=date(2025, 12, 31),
            filing_at=datetime(2026, 4, 20, tzinfo=UTC),
            available_at=datetime(2026, 4, 20, tzinfo=UTC),
        ),
    )

    for value in cases:
        signal = PEADStrategy().evaluate(INSTRUMENT_ID, AS_OF, (value,))
        assert signal.value == 0
        assert signal.direction is SignalDirection.NEUTRAL
        assert signal.evidence_refs == ()
        assert "no_qualifying_earnings_event" in signal.reason_codes


def test_duplicate_report_period_prefers_available_8k_deterministically() -> None:
    ten_q = event(
        event_id=UUID(int=20),
        evidence_id=UUID(int=21),
        source_type=FilingSource.TEN_Q,
        surprise=EarningsSurprise.MISS,
        available_at=datetime(2026, 5, 1, 10, tzinfo=UTC),
        filing_at=datetime(2026, 5, 1, 10, tzinfo=UTC),
    )
    eight_k = event(
        event_id=UUID(int=22),
        evidence_id=UUID(int=23),
        source_type=FilingSource.EIGHT_K,
        surprise=EarningsSurprise.BEAT,
    )

    first = PEADStrategy().evaluate(INSTRUMENT_ID, AS_OF, (ten_q, eight_k))
    second = PEADStrategy().evaluate(INSTRUMENT_ID, AS_OF, (eight_k, ten_q))

    assert first == second
    assert first.direction is SignalDirection.LONG
    assert first.evidence_refs == (eight_k.evidence_id,)
    assert "source:8-K" in first.reason_codes


def test_equivalent_as_of_offsets_produce_identical_signal_identity() -> None:
    utc = PEADStrategy().evaluate(INSTRUMENT_ID, AS_OF, (event(),))
    offset = PEADStrategy().evaluate(
        INSTRUMENT_ID,
        AS_OF.astimezone(timezone(timedelta(hours=8))),
        (event(),),
    )

    assert offset == utc


def test_other_instrument_and_meet_never_form_directional_view() -> None:
    other = event(instrument_id=UUID(int=99), surprise=EarningsSurprise.MISS)
    meet = event(event_id=UUID(int=30), surprise=EarningsSurprise.MEET)

    signal = PEADStrategy().evaluate(INSTRUMENT_ID, AS_OF, (other, meet))

    assert signal.value == 0
    assert signal.direction is SignalDirection.NEUTRAL


def test_event_contract_rejects_impossible_timeline_and_extra_authority() -> None:
    with pytest.raises(ValidationError):
        event(available_at=datetime(2026, 4, 30, tzinfo=UTC))
    with pytest.raises(ValidationError):
        event(order={"quantity": 100})


def test_strategy_manifest_is_draft_pinned_and_has_no_execution_authority() -> None:
    manifest = yaml.safe_load(Path("strategies/manifest.yaml").read_text("utf-8"))
    pead = manifest["strategies"][0]

    assert pead["strategy_id"] == "pead"
    assert pead["state"] == "draft"
    assert pead["upstream_commit"] == "3a18702cb25777fb4bdb4b2527a0c868bc8297f4"
    assert pead["notice_id"] == "AI-HEDGE-FUND-MIT-PEAD-EVENT-STUDY"
    assert set(pead["output_authority"]) == {"alpha_signal"}
    assert "order" not in json.dumps(pead).lower()
    notice = Path("docs/legal/notices/AI-HEDGE-FUND-MIT-PEAD-EVENT-STUDY.md").read_text(
        "utf-8"
    )
    assert "Copyright (c) 2024 Virat Singh" in notice
    assert "3a18702cb25777fb4bdb4b2527a0c868bc8297f4" in notice
    assert "MIT License" in notice
