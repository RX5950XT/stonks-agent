from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.adapters.llm.fake import FakeLLMOutput, FakeStructuredLLMAdapter
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.model_policy import ModelPolicy, load_model_policy
from stonks_agent.domain.research import LLMMessage, LLMRole, StructuredLLMRequest
from stonks_agent.domain.usage_budget import UsageConsumption

NOW = datetime(2026, 7, 12, 12, tzinfo=UTC)


def request(**overrides: object) -> StructuredLLMRequest:
    values: dict[str, object] = {
        "request_id": UUID("00000000-0000-4000-8000-000000000101"),
        "model": "policy:models-v1",
        "messages": (LLMMessage(role=LLMRole.USER, content="Return an answer"),),
        "output_schema_name": "answer",
        "output_schema_version": "1.0.0",
        "output_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["answer"],
            "properties": {"answer": {"type": "string", "minLength": 1}},
        },
        "max_output_tokens": 100,
        "deadline_at": NOW + timedelta(minutes=1),
    }
    values.update(overrides)
    return StructuredLLMRequest.model_validate(values)


def output(
    parsed: dict[str, object],
    *,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> FakeLLMOutput:
    return FakeLLMOutput(
        parsed_output=parsed,
        usage=UsageConsumption(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            elapsed_ms=3,
        ),
    )


def adapter(
    store: MemoryArtifactStore,
    *outputs: FakeLLMOutput,
) -> FakeStructuredLLMAdapter:
    return FakeStructuredLLMAdapter(
        policy=load_model_policy("config/models.yaml"),
        artifacts=store,
        outputs=outputs,
        clock=lambda: NOW,
    )


def test_fake_validates_schema_archives_before_success_and_accounts_cost() -> None:
    store = MemoryArtifactStore()
    llm = adapter(store, output({"answer": "evidence first"}))

    result = llm.complete(request())

    assert isinstance(result, Success)
    response = result.value
    assert response.parsed_output == {"answer": "evidence first"}
    assert response.model == "fake-structured-v1"
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 5
    assert response.usage.cost_usd == Decimal("0")
    raw = b'{"answer":"evidence first"}'
    digest = hashlib.sha256(raw).hexdigest()
    assert response.raw_output_artifact_ref == f"sha256:{digest}"
    assert store.is_finalized(digest)


def test_fake_repairs_invalid_output_once_and_aggregates_all_billed_usage() -> None:
    store = MemoryArtifactStore()
    llm = adapter(
        store,
        output({"wrong": True}, input_tokens=11, output_tokens=2),
        output({"answer": "repaired"}, input_tokens=13, output_tokens=4),
    )

    result = llm.complete(request())

    assert isinstance(result, Success)
    assert result.value.parsed_output == {"answer": "repaired"}
    assert result.value.usage.input_tokens == 24
    assert result.value.usage.output_tokens == 6
    invalid_raw = b'{"wrong":true}'
    assert store.is_finalized(hashlib.sha256(invalid_raw).hexdigest())


def test_fake_still_invalid_after_repair_returns_structured_failure_with_usage() -> (
    None
):
    store = MemoryArtifactStore()
    llm = adapter(store, output({"wrong": 1}), output({"wrong": 2}))

    result = llm.complete(request())

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.MODEL_OUTPUT_INVALID
    assert result.error.details["reason"] == "schema_validation_failed"
    assert result.error.details["attempts"] == 2
    assert result.error.details["usage"] == {
        "iterations": 0,
        "tool_calls": 0,
        "input_tokens": 20,
        "output_tokens": 10,
        "cost_usd": "0",
        "elapsed_ms": 6,
    }


def test_fake_rejects_unknown_model_deadline_and_output_limit_before_consumption() -> (
    None
):
    for invalid_request, code in (
        (request(model="unknown-model"), ErrorCode.CONFIGURATION_INVALID),
        (request(deadline_at=NOW - timedelta(seconds=1)), ErrorCode.DEADLINE_EXCEEDED),
        (request(max_output_tokens=100_001), ErrorCode.BUDGET_EXHAUSTED),
    ):
        store = MemoryArtifactStore()
        llm = adapter(store, output({"answer": "must not be consumed"}))

        result = llm.complete(invalid_request)

        assert isinstance(result, Failure)
        assert result.error.code is code
        assert llm.remaining_outputs == 1


def test_fake_exhausted_script_and_artifact_failure_never_report_success() -> None:
    exhausted = adapter(MemoryArtifactStore())
    exhausted_result = exhausted.complete(request())
    tiny_store = MemoryArtifactStore(max_size_bytes=1)
    archive_result = adapter(tiny_store, output({"answer": "too large"})).complete(
        request()
    )

    assert isinstance(exhausted_result, Failure)
    assert exhausted_result.error.code is ErrorCode.DATA_UNAVAILABLE
    assert isinstance(archive_result, Failure)
    assert archive_result.error.code is ErrorCode.INTERNAL_ERROR


def test_invalid_json_schema_and_route_token_budget_fail_closed() -> None:
    invalid_schema = adapter(MemoryArtifactStore(), output({"answer": "unused"}))
    invalid_result = invalid_schema.complete(
        request(output_schema={"type": "not-a-json-schema-type"})
    )

    loaded = load_model_policy("config/models.yaml")
    fake_route = loaded.resolve("policy:models-v1")
    bounded_route = fake_route.model_copy(update={"max_total_tokens_per_request": 1})
    bounded_policy = ModelPolicy(
        policy_id="bounded-models-v1",
        routes=(bounded_route,),
    )
    bounded = FakeStructuredLLMAdapter(
        policy=bounded_policy,
        artifacts=MemoryArtifactStore(),
        outputs=(output({"answer": "billed"}),),
        clock=lambda: NOW,
    )
    budget_result = bounded.complete(request())

    assert isinstance(invalid_result, Failure)
    assert invalid_result.error.code is ErrorCode.INVALID_INPUT
    assert invalid_schema.remaining_outputs == 1
    assert isinstance(budget_result, Failure)
    assert budget_result.error.code is ErrorCode.BUDGET_EXHAUSTED
    assert budget_result.error.details["exceeded"] == ("total_tokens",)
    assert budget_result.error.details["usage"]["input_tokens"] == 10


def test_output_that_arrives_after_deadline_is_archived_billed_and_rejected() -> None:
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return NOW if calls <= 3 else NOW + timedelta(minutes=2)

    store = MemoryArtifactStore()
    llm = FakeStructuredLLMAdapter(
        policy=load_model_policy("config/models.yaml"),
        artifacts=store,
        outputs=(output({"answer": "late"}),),
        clock=clock,
    )

    result = llm.complete(request())

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.DEADLINE_EXCEEDED
    assert result.error.details["usage"]["input_tokens"] == 10
    raw = b'{"answer":"late"}'
    assert store.is_finalized(hashlib.sha256(raw).hexdigest())
