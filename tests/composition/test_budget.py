from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from stonks_agent.composition.budget import (
    MonotonicBudgetUsage,
    UsageTrackingLLM,
)
from stonks_agent.domain.errors import Success
from stonks_agent.domain.operational_budget import BudgetScope
from stonks_agent.domain.research import (
    LLMMessage,
    LLMRole,
    StructuredLLMRequest,
    StructuredLLMResponse,
)
from stonks_agent.domain.usage_budget import UsageConsumption

NOW = datetime(2026, 7, 28, 5, tzinfo=UTC)


class LLM:
    def complete(self, request: StructuredLLMRequest) -> Success[StructuredLLMResponse]:
        return Success(
            StructuredLLMResponse(
                request_id=request.request_id,
                model="actual-model",
                parsed_output={"ok": True},
                raw_output_artifact_ref=f"sha256:{'a' * 64}",
                usage=UsageConsumption(
                    input_tokens=10,
                    output_tokens=5,
                    cost_usd=Decimal("0.125"),
                    elapsed_ms=15,
                ),
                created_at=NOW,
            )
        )


def request() -> StructuredLLMRequest:
    return StructuredLLMRequest(
        request_id=UUID("74100000-0000-4000-8000-000000000001"),
        model="policy:research-models-v1",
        messages=(LLMMessage(role=LLMRole.USER, content="test"),),
        output_schema_name="test",
        output_schema_version="1.0.0",
        output_schema={"type": "object"},
        max_output_tokens=10,
        deadline_at=NOW + timedelta(minutes=1),
    )


def test_tracking_llm_records_decimal_cost_on_one_monotonic_clock() -> None:
    moments = iter((10.0, 12.5))
    usage = MonotonicBudgetUsage(monotonic_clock=lambda: next(moments))

    result = UsageTrackingLLM(LLM(), usage).complete(request())
    snapshot = usage.snapshot(BudgetScope.RESEARCH)

    assert isinstance(result, Success)
    assert snapshot.cost_usd == Decimal("0.125")
    assert snapshot.monotonic_started_seconds == Decimal("10.0")
    assert snapshot.monotonic_observed_seconds == Decimal("12.5")
