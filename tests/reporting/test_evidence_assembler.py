from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_agent.application.reporting.evidence_assembler import (
    assemble_evidence_context,
)
from stonks_agent.domain.analysis_context import (
    AnalysisContextRequest,
    EvidenceRequirement,
)
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError, Success
from stonks_contracts.evidence import EvidenceItem, EvidenceKind, Sensitivity
from stonks_contracts.market_data import DataQuality, DataQualityStatus

AS_OF = datetime(2026, 7, 13, 4, tzinfo=UTC)
CONTEXT_ID = UUID("32000000-0000-4000-8000-000000000001")
RUN_ID = UUID("32000000-0000-4000-8000-000000000002")
MARKET_ID = UUID("32000000-0000-4000-8000-000000000003")
NEWS_ID = UUID("32000000-0000-4000-8000-000000000004")


def evidence(
    evidence_id: UUID,
    *,
    kind: EvidenceKind,
    available_at: datetime = AS_OF - timedelta(minutes=5),
    event_time: datetime | None = None,
    quality_status: DataQualityStatus = DataQualityStatus.AVAILABLE,
    completeness: str = "1",
    content_hash: str = "a" * 64,
    sensitivity: Sensitivity = Sensitivity.PUBLIC,
    license_tag: str = "Apache-2.0",
    redistribution_tag: str = "internal-use",
    untrusted_content: bool = False,
    evidence_as_of: datetime = AS_OF,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        subject="AAPL",
        kind=kind,
        payload={"value": "redacted-in-public-summary"},
        event_time=event_time or available_at,
        published_at=available_at - timedelta(seconds=1),
        available_at=available_at,
        observed_at=max(available_at, evidence_as_of),
        as_of=evidence_as_of,
        source="fixture",
        provider="replay",
        content_hash=content_hash,
        raw_artifact_ref=f"sha256:{content_hash}",
        quality=DataQuality(
            status=quality_status,
            completeness=Decimal(completeness),
        ),
        sensitivity=sensitivity,
        license_tag=license_tag,
        redistribution_tag=redistribution_tag,
        untrusted_content=untrusted_content,
    )


def request(**overrides: object) -> AnalysisContextRequest:
    values: dict[str, object] = {
        "context_id": CONTEXT_ID,
        "run_id": RUN_ID,
        "subject": "AAPL",
        "as_of": AS_OF,
        "requirements": (
            EvidenceRequirement(
                capability="market",
                kinds=(EvidenceKind.MARKET_DATA,),
                required=True,
                minimum_items=1,
                maximum_items=10,
                freshness_seconds=3600,
            ),
            EvidenceRequirement(
                capability="news",
                kinds=(EvidenceKind.NEWS,),
                required=False,
                minimum_items=0,
                maximum_items=10,
                freshness_seconds=7200,
            ),
        ),
        "allowed_sensitivities": (Sensitivity.PUBLIC, Sensitivity.INTERNAL),
        "allowed_license_tags": ("Apache-2.0", "MIT"),
        "allowed_redistribution_tags": ("internal-use",),
    }
    values.update(overrides)
    return AnalysisContextRequest.model_validate(values)


class Repository:
    def __init__(self, result: object) -> None:
        self.result = result
        self.queries: list[tuple[str, datetime]] = []

    def query_available(self, *, subject: str, as_of: datetime) -> object:
        self.queries.append((subject, as_of))
        return self.result


def test_available_context_is_read_once_sorted_and_hash_stable() -> None:
    market = evidence(MARKET_ID, kind=EvidenceKind.MARKET_DATA)
    news = evidence(
        NEWS_ID,
        kind=EvidenceKind.NEWS,
        available_at=AS_OF - timedelta(minutes=1),
        untrusted_content=True,
    )
    repository = Repository(Success((news, market)))

    first = assemble_evidence_context(request(), repository)  # type: ignore[arg-type]
    second = assemble_evidence_context(request(), Repository(Success((market, news))))  # type: ignore[arg-type]

    assert isinstance(first, Success)
    assert isinstance(second, Success)
    assert first.value == second.value
    assert first.value.payload_hash == second.value.payload_hash
    assert tuple(item.evidence_id for item in first.value.evidence) == (
        MARKET_ID,
        NEWS_ID,
    )
    assert first.value.blocks[0].status is DataQualityStatus.AVAILABLE
    assert first.value.blocks[1].status is DataQualityStatus.AVAILABLE
    assert first.value.evidence[1].untrusted_content is True
    assert repository.queries == [("AAPL", AS_OF)]


def test_missing_stale_not_supported_and_partial_are_explicit_not_fake_success() -> (
    None
):
    stale_market = evidence(
        MARKET_ID,
        kind=EvidenceKind.MARKET_DATA,
        available_at=AS_OF - timedelta(hours=2),
    )
    partial_news = evidence(
        NEWS_ID,
        kind=EvidenceKind.NEWS,
        quality_status=DataQualityStatus.PARTIAL,
        completeness="0.5",
    )
    unsupported = EvidenceRequirement(
        capability="chips",
        kinds=(EvidenceKind.DERIVED,),
        required=False,
        supported=False,
        minimum_items=0,
        maximum_items=1,
    )
    missing = EvidenceRequirement(
        capability="filing",
        kinds=(EvidenceKind.FILING,),
        required=True,
        minimum_items=1,
        maximum_items=5,
    )
    value = request(requirements=(*request().requirements, unsupported, missing))

    result = assemble_evidence_context(
        value,
        Repository(Success((stale_market, partial_news))),  # type: ignore[arg-type]
    )

    assert isinstance(result, Success)
    statuses = {block.capability: block.status for block in result.value.blocks}
    assert statuses == {
        "chips": DataQualityStatus.NOT_SUPPORTED,
        "filing": DataQualityStatus.MISSING,
        "market": DataQualityStatus.STALE,
        "news": DataQualityStatus.PARTIAL,
    }
    assert "required:filing:missing" in result.value.data_limitations
    assert "market:stale" in result.value.data_limitations


def test_conflicting_same_event_facts_remain_visible_as_conflict() -> None:
    first = evidence(
        MARKET_ID,
        kind=EvidenceKind.MARKET_DATA,
        event_time=AS_OF - timedelta(minutes=10),
        content_hash="a" * 64,
    )
    second = evidence(
        UUID(int=40),
        kind=EvidenceKind.MARKET_DATA,
        event_time=first.event_time,
        content_hash="b" * 64,
    )

    result = assemble_evidence_context(
        request(),
        Repository(Success((first, second))),  # type: ignore[arg-type]
    )

    assert isinstance(result, Success)
    market = next(
        block for block in result.value.blocks if block.capability == "market"
    )
    assert market.status is DataQualityStatus.CONFLICT
    assert set(market.evidence_refs) == {first.evidence_id, second.evidence_id}
    assert "market:conflict" in result.value.data_limitations


def test_future_or_wrong_subject_repository_output_fails_closed() -> None:
    future = evidence(
        MARKET_ID,
        kind=EvidenceKind.MARKET_DATA,
        available_at=AS_OF + timedelta(seconds=1),
        evidence_as_of=AS_OF + timedelta(seconds=1),
    )
    wrong = evidence(MARKET_ID, kind=EvidenceKind.MARKET_DATA).model_copy(
        update={"subject": "MSFT"}
    )

    for item in (future, wrong):
        result = assemble_evidence_context(
            request(),
            Repository(Success((item,))),  # type: ignore[arg-type]
        )
        assert isinstance(result, Failure)
        assert result.error.code is ErrorCode.CONFLICT


def test_sensitivity_license_and_redistribution_policy_exclude_payloads() -> None:
    restricted = evidence(
        MARKET_ID,
        kind=EvidenceKind.MARKET_DATA,
        sensitivity=Sensitivity.RESTRICTED,
    )
    wrong_license = evidence(
        UUID(int=50),
        kind=EvidenceKind.MARKET_DATA,
        license_tag="Proprietary",
    )
    wrong_redistribution = evidence(
        UUID(int=51),
        kind=EvidenceKind.MARKET_DATA,
        redistribution_tag="no-internal-use",
    )

    result = assemble_evidence_context(
        request(),
        Repository(Success((restricted, wrong_license, wrong_redistribution))),  # type: ignore[arg-type]
    )

    assert isinstance(result, Success)
    assert result.value.evidence == ()
    assert result.value.blocks[0].status is DataQualityStatus.MISSING
    assert "market:policy_excluded:3" in result.value.data_limitations


def test_repository_failure_propagates_without_empty_context() -> None:
    unavailable = Failure(
        StructuredError(ErrorCode.DATA_UNAVAILABLE, "evidence DB unavailable")
    )

    result = assemble_evidence_context(request(), Repository(unavailable))  # type: ignore[arg-type]

    assert result is unavailable


def test_request_rejects_duplicate_capabilities_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        request(requirements=(request().requirements[0], request().requirements[0]))
    with pytest.raises(ValidationError):
        request(fetch_url="https://provider.example")


def test_context_contract_rejects_block_refs_outside_evidence() -> None:
    assembled = assemble_evidence_context(
        request(),
        Repository(Success((evidence(MARKET_ID, kind=EvidenceKind.MARKET_DATA),))),  # type: ignore[arg-type]
    )
    assert isinstance(assembled, Success)
    payload = assembled.value.model_dump(mode="python")
    payload["evidence"] = ()

    with pytest.raises(ValidationError, match="exactly cover"):
        type(assembled.value).model_validate(payload)
