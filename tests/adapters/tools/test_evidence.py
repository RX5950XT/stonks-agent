from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.adapters.tools.evidence import (
    EvidenceTool,
    build_evidence_tool_policy,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.tool_policy import (
    AuthorizedToolCall,
    ToolMutationClass,
    ToolResult,
)
from stonks_contracts.evidence import EvidenceItem, EvidenceKind
from stonks_contracts.market_data import DataQuality, DataQualityStatus

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000000003")
INSTRUMENT = "instrument:aapl"


class EvidenceRepository:
    def __init__(self, items: tuple[EvidenceItem, ...]) -> None:
        self._items = {item.evidence_id: item for item in items}

    def get(self, evidence_id: UUID) -> Success[EvidenceItem] | Failure:
        item = self._items.get(evidence_id)
        if item is None:
            from stonks_agent.domain.errors import StructuredError

            return Failure(
                StructuredError(code=ErrorCode.NOT_FOUND, message="not found")
            )
        return Success(item)


def evidence(
    *,
    evidence_id: UUID = EVIDENCE_ID,
    available_at: datetime = NOW - timedelta(minutes=1),
    close: str = "101",
    event_time: datetime = NOW - timedelta(days=1),
) -> EvidenceItem:
    high = str(max(Decimal("102"), Decimal(close)))
    item_as_of = max(NOW, available_at)
    return EvidenceItem(
        evidence_id=evidence_id,
        subject=INSTRUMENT,
        kind=EvidenceKind.MARKET_DATA,
        payload={
            "open": "100",
            "high": high,
            "low": "99",
            "close": close,
            "volume": "1000",
        },
        event_time=event_time,
        published_at=event_time,
        available_at=available_at,
        observed_at=item_as_of,
        as_of=item_as_of,
        source="openbb",
        provider="yfinance",
        content_hash="a" * 64,
        raw_artifact_ref=f"sha256:{'a' * 64}",
        quality=DataQuality(
            status=DataQualityStatus.AVAILABLE,
            completeness=Decimal("1"),
        ),
        license_tag="provider-terms",
        redistribution_tag="internal",
        untrusted_content=True,
    )


def call(
    tool_name: str,
    *,
    arguments: dict[str, object] | None = None,
    evidence_ids: frozenset[UUID] = frozenset({EVIDENCE_ID}),
) -> AuthorizedToolCall:
    return AuthorizedToolCall(
        call_id=uuid4(),
        policy_id="research-evidence-v1",
        principal_subject="research-worker",
        principal_profile="research-worker",
        tool_name=tool_name,
        arguments=arguments or {},
        audit_arguments=arguments or {},
        instrument_ids=frozenset({INSTRUMENT}),
        evidence_ids=evidence_ids,
        timeout_ms=2_000,
        output_limit_bytes=16_384,
    )


def payload(
    result: Success[ToolResult],
    artifacts: MemoryArtifactStore,
) -> dict[str, object]:
    tool_result = result.value
    stored = artifacts.read(tool_result.content_hash)
    assert isinstance(stored, Success)
    return json.loads(stored.value)


def test_policy_exposes_only_three_read_only_scoped_tools() -> None:
    policy = build_evidence_tool_policy(
        instrument_ids=frozenset({INSTRUMENT}),
        evidence_ids=frozenset({EVIDENCE_ID}),
    )

    assert {rule.name for rule in policy.tools} == {
        "list_evidence",
        "read_evidence",
        "price_window",
    }
    assert all(
        rule.mutation_class is ToolMutationClass.READ_ONLY for rule in policy.tools
    )
    assert all(rule.audit_enabled for rule in policy.tools)


def test_list_and_read_never_escape_the_authorized_evidence_scope() -> None:
    other = evidence(evidence_id=uuid4(), close="999")
    artifacts = MemoryArtifactStore()
    tool = EvidenceTool(
        repository=EvidenceRepository((evidence(), other)),
        artifacts=artifacts,
        as_of=NOW,
    )

    listed = tool.execute(call("list_evidence"))
    read = tool.execute(
        call("read_evidence", arguments={"evidence_id": str(EVIDENCE_ID)})
    )

    assert isinstance(listed, Success)
    assert isinstance(read, Success)
    listed_payload = payload(listed, artifacts)
    read_payload = payload(read, artifacts)
    assert listed_payload["count"] == 1
    assert listed_payload["items"][0]["evidence_id"] == str(EVIDENCE_ID)
    assert read_payload["evidence_id"] == str(EVIDENCE_ID)
    assert "999" not in json.dumps((listed_payload, read_payload))
    assert listed.value.materialized_evidence_ids == frozenset()
    assert read.value.materialized_evidence_ids == frozenset({EVIDENCE_ID})


def test_future_or_unscoped_evidence_fails_closed() -> None:
    future = evidence(available_at=NOW + timedelta(minutes=1))
    tool = EvidenceTool(
        repository=EvidenceRepository((future,)),
        artifacts=MemoryArtifactStore(),
        as_of=NOW,
    )

    future_result = tool.execute(
        call("read_evidence", arguments={"evidence_id": str(EVIDENCE_ID)})
    )
    unscoped_result = tool.execute(
        call(
            "read_evidence",
            arguments={"evidence_id": str(uuid4())},
        )
    )

    assert isinstance(future_result, Failure)
    assert future_result.error.code is ErrorCode.CAPABILITY_DENIED
    assert isinstance(unscoped_result, Failure)
    assert unscoped_result.error.code is ErrorCode.CAPABILITY_DENIED


def test_price_window_returns_bounded_statistics_from_scoped_bars() -> None:
    first = evidence(close="100", event_time=NOW - timedelta(days=2))
    second = evidence(
        evidence_id=UUID("00000000-0000-4000-8000-000000000004"),
        close="110",
        event_time=NOW - timedelta(days=1),
    )
    outside_window = evidence(
        evidence_id=UUID("00000000-0000-4000-8000-000000000005"),
        close="90",
        event_time=NOW - timedelta(days=4),
    )
    artifacts = MemoryArtifactStore()
    tool = EvidenceTool(
        repository=EvidenceRepository((first, second, outside_window)),
        artifacts=artifacts,
        as_of=NOW,
    )

    result = tool.execute(
        call(
            "price_window",
            arguments={
                "start": (NOW - timedelta(days=3)).isoformat(),
                "end": NOW.isoformat(),
            },
            evidence_ids=frozenset(
                {first.evidence_id, second.evidence_id, outside_window.evidence_id}
            ),
        )
    )

    assert isinstance(result, Success)
    value = payload(result, artifacts)
    assert value["count"] == 2
    assert value["statistics"]["return"] == "0.1"
    assert value["statistics"]["high"] == "110"
    assert value["statistics"]["low"] == "99"
    assert result.value.materialized_evidence_ids == frozenset(
        {first.evidence_id, second.evidence_id}
    )
