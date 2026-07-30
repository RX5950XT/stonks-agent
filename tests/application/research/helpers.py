from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.research import (
    ResearchRequest,
    StructuredLLMRequest,
    StructuredLLMResponse,
)
from stonks_agent.domain.tool_policy import AuthorizedToolCall, ToolResult
from stonks_agent.domain.usage_budget import UsageBudget, UsageConsumption
from stonks_contracts.evidence import EvidenceItem, EvidenceKind
from stonks_contracts.market_data import DataQuality, DataQualityStatus

NOW = datetime(2026, 7, 12, 12, tzinfo=UTC)
INSTRUMENT = "instrument:aapl"
OTHER_INSTRUMENT = "instrument:msft"
EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000000003")


def artifact_ref(character: str) -> str:
    return f"sha256:{character * 64}"


def request(**overrides: object) -> ResearchRequest:
    values: dict[str, object] = {
        "request_id": UUID("00000000-0000-4000-8000-000000000010"),
        "run_id": UUID("00000000-0000-4000-8000-000000000011"),
        "instrument_ids": frozenset({INSTRUMENT}),
        "as_of": NOW,
        "horizon_days": 20,
        "question": "What changed?",
        "allowed_evidence_ids": frozenset({EVIDENCE_ID}),
        "tool_policy_id": "research-tools-v1",
        "model_policy_id": "models-v1",
        "budget": UsageBudget(
            max_iterations=4,
            max_tool_calls=4,
            max_input_tokens=1_000,
            max_output_tokens=500,
            max_total_tokens=1_500,
            max_cost_usd=Decimal("2"),
            max_elapsed_ms=30_000,
        ),
        "created_at": NOW,
        "deadline_at": NOW + timedelta(minutes=1),
    }
    values.update(overrides)
    return ResearchRequest.model_validate(values)


def evidence(**overrides: object) -> EvidenceItem:
    values: dict[str, object] = {
        "evidence_id": EVIDENCE_ID,
        "subject": INSTRUMENT,
        "kind": EvidenceKind.FILING,
        "payload": {"form": "10-Q"},
        "event_time": NOW - timedelta(days=1),
        "published_at": NOW - timedelta(hours=2),
        "available_at": NOW - timedelta(hours=1),
        "observed_at": NOW,
        "as_of": NOW,
        "source": "fixture",
        "provider": "replay",
        "content_hash": "a" * 64,
        "raw_artifact_ref": artifact_ref("a"),
        "quality": DataQuality(
            status=DataQualityStatus.AVAILABLE,
            completeness=Decimal("1"),
        ),
        "license_tag": "fixture",
        "redistribution_tag": "internal",
        "untrusted_content": True,
    }
    values.update(overrides)
    return EvidenceItem.model_validate(values)


class DictArtifactReader:
    def __init__(self, content: dict[str, bytes]) -> None:
        self.content = content

    def read(self, content_hash: str) -> Result[bytes]:
        value = self.content.get(content_hash)
        if value is None:
            return Failure(
                StructuredError(
                    code=ErrorCode.NOT_FOUND,
                    message="Artifact not found",
                )
            )
        return Success(value)


class ScriptedLLM:
    def __init__(self, outputs: list[dict[str, object]]) -> None:
        self.outputs = outputs
        self.requests: list[StructuredLLMRequest] = []

    def complete(
        self,
        request: StructuredLLMRequest,
    ) -> Result[StructuredLLMResponse]:
        self.requests.append(request)
        index = len(self.requests) - 1
        return Success(
            StructuredLLMResponse(
                request_id=request.request_id,
                model=request.model,
                parsed_output=self.outputs[index],
                raw_output_artifact_ref=artifact_ref(chr(ord("c") + index)),
                usage=UsageConsumption(
                    input_tokens=10,
                    output_tokens=5,
                    cost_usd=Decimal("0.01"),
                    elapsed_ms=10,
                ),
                created_at=NOW,
            )
        )


class FailingLLM:
    def complete(
        self,
        request: StructuredLLMRequest,
    ) -> Result[StructuredLLMResponse]:
        return Failure(
            StructuredError(
                code=ErrorCode.DATA_UNAVAILABLE,
                message="LLM unavailable",
            )
        )


class RecordingTool:
    def __init__(self) -> None:
        self.calls: list[AuthorizedToolCall] = []

    def execute(self, call: AuthorizedToolCall) -> Result[ToolResult]:
        self.calls.append(call)
        character = "e" if call.arguments["query"] == "first" else "f"
        return Success(
            ToolResult(
                call_id=call.call_id,
                artifact_ref=artifact_ref(character),
                content_hash=character * 64,
                content_type="application/json",
                byte_count=20,
                tool_version="fixture/1",
                materialized_evidence_ids=call.evidence_ids,
                latency_ms=5,
                observed_at=NOW,
            )
        )
