from __future__ import annotations

from datetime import timedelta
from threading import Barrier
from time import monotonic, sleep
from uuid import UUID

from stonks_agent.application.research.context_builder import build_research_context
from stonks_agent.application.research.tool_loop import run_tool_loop
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.tool_policy import (
    ResearchPrincipal,
    ToolArgumentKind,
    ToolArgumentSpec,
    ToolMutationClass,
    ToolPolicy,
    ToolRule,
)

from .helpers import (
    EVIDENCE_ID,
    INSTRUMENT,
    NOW,
    OTHER_INSTRUMENT,
    DictArtifactReader,
    FailingLLM,
    RecordingTool,
    ScriptedLLM,
    evidence,
    request,
)


def principal() -> ResearchPrincipal:
    return ResearchPrincipal(
        subject="research-worker-1",
        profile="research-worker",
        tool_policy_id="research-tools-v1",
    )


def policy() -> ToolPolicy:
    return ToolPolicy(
        policy_id="research-tools-v1",
        principal_profile="research-worker",
        tools=(
            ToolRule(
                name="evidence.lookup",
                mutation_class=ToolMutationClass.READ_ONLY,
                arguments=(
                    ToolArgumentSpec(
                        name="query",
                        kind=ToolArgumentKind.STRING,
                        max_length=64,
                    ),
                ),
                max_timeout_ms=1_000,
                max_output_bytes=1_024,
                audit_enabled=True,
            ),
        ),
        allowed_instrument_ids=frozenset({INSTRUMENT}),
        allowed_evidence_ids=frozenset({EVIDENCE_ID}),
    )


def tool_turn(*queries: str) -> dict[str, object]:
    return {
        "action": "tools",
        "tool_calls": [
            {
                "tool_name": "evidence.lookup",
                "arguments": {"query": query},
                "instrument_ids": [INSTRUMENT],
                "evidence_ids": [str(EVIDENCE_ID)],
                "timeout_ms": 1_000,
                "output_limit_bytes": 1_024,
            }
            for query in queries
        ],
        "claims": [],
        "confidence": None,
        "counterarguments": [],
        "risks": [],
        "warnings": [],
    }


def final_turn() -> dict[str, object]:
    return {
        "action": "final",
        "tool_calls": [],
        "claims": [
            {"text": "Revenue increased.", "evidence_ids": [str(EVIDENCE_ID)]},
            {"text": "Demand may improve.", "evidence_ids": []},
        ],
        "confidence": "0.7",
        "counterarguments": ["Margins remain volatile."],
        "risks": ["Demand slowdown."],
        "warnings": [],
    }


def hypothesis_only_final_turn() -> dict[str, object]:
    value = final_turn()
    value["claims"] = [
        {"text": "Demand may improve.", "evidence_ids": []},
    ]
    return value


def context_and_reader() -> tuple[object, DictArtifactReader]:
    reader = DictArtifactReader(
        {
            "a" * 64: b'{"source":"filing"}',
            "e" * 64: b'{"tool":"first"}',
            "f" * 64: b'{"tool":"second"}',
        }
    )
    context = build_research_context(request(), (evidence(),), reader)
    assert isinstance(context, Success)
    return context.value, reader


def test_loop_executes_authorized_read_tools_then_returns_typed_final_draft() -> None:
    context, reader = context_and_reader()
    llm = ScriptedLLM([tool_turn("first", "second"), final_turn()])
    tool = RecordingTool()

    result = run_tool_loop(
        request=request(),
        context=context,
        principal=principal(),
        policy=policy(),
        llm=llm,
        tool=tool,
        artifacts=reader,
        clock=lambda: NOW,
    )

    assert isinstance(result, Success)
    assert len(tool.calls) == 2
    assert result.value.usage.iterations == 2
    assert result.value.usage.tool_calls == 2
    assert result.value.tool_output_artifact_refs == (
        f"sha256:{'e' * 64}",
        f"sha256:{'f' * 64}",
    )
    assert len(llm.requests[1].untrusted_blocks) == 3
    assert all(
        '"tool":"first"' not in message.content for message in llm.requests[1].messages
    )


def test_model_schema_and_prompt_describe_only_the_scoped_read_tool() -> None:
    context, reader = context_and_reader()
    llm = ScriptedLLM([hypothesis_only_final_turn()])

    result = run_tool_loop(
        request=request(),
        context=context,
        principal=principal(),
        policy=policy(),
        llm=llm,
        tool=RecordingTool(),
        artifacts=reader,
        clock=lambda: NOW,
    )

    assert isinstance(result, Success)
    llm_request = llm.requests[0]
    prompt = llm_request.messages[0].content
    assert "evidence.lookup" in prompt
    assert "read-only" in prompt
    assert "untrusted data" in prompt
    assert "shell.exec" not in prompt

    schema = llm_request.output_schema
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])  # type: ignore[arg-type]
    calls = schema["properties"]["tool_calls"]  # type: ignore[index]
    variant = calls["items"]["anyOf"][0]  # type: ignore[index]
    assert variant["additionalProperties"] is False
    assert set(variant["required"]) == set(variant["properties"])
    assert variant["properties"]["tool_name"] == {"const": "evidence.lookup"}
    arguments = variant["properties"]["arguments"]
    assert arguments["additionalProperties"] is False
    assert set(arguments["required"]) == {"query"}


def test_final_claim_cannot_cite_evidence_that_was_not_materialized() -> None:
    context, reader = context_and_reader()
    tool = RecordingTool()

    result = run_tool_loop(
        request=request(),
        context=context,
        principal=principal(),
        policy=policy(),
        llm=ScriptedLLM([final_turn()]),
        tool=tool,
        artifacts=reader,
        clock=lambda: NOW,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.MODEL_OUTPUT_INVALID
    assert tool.calls == []


def test_metadata_only_tool_result_does_not_materialize_evidence() -> None:
    context, reader = context_and_reader()

    class MetadataOnlyTool(RecordingTool):
        def execute(self, call: object) -> object:
            outcome = super().execute(call)  # type: ignore[arg-type]
            assert isinstance(outcome, Success)
            return Success(
                outcome.value.model_copy(
                    update={"materialized_evidence_ids": frozenset()}
                )
            )

    result = run_tool_loop(
        request=request(),
        context=context,
        principal=principal(),
        policy=policy(),
        llm=ScriptedLLM([tool_turn("first"), final_turn()]),
        tool=MetadataOnlyTool(),  # type: ignore[arg-type]
        artifacts=reader,
        clock=lambda: NOW,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.MODEL_OUTPUT_INVALID


def test_final_claim_citations_must_be_a_subset_of_materialized_evidence() -> None:
    context, reader = context_and_reader()
    other_evidence_id = UUID("00000000-0000-4000-8000-000000000004")
    wider_request = request(
        allowed_evidence_ids=frozenset({EVIDENCE_ID, other_evidence_id})
    )
    wider_policy = policy().model_copy(
        update={"allowed_evidence_ids": frozenset({EVIDENCE_ID, other_evidence_id})}
    )
    unsupported_final = final_turn()
    unsupported_final["claims"] = [
        {"text": "Unsupported claim.", "evidence_ids": [str(other_evidence_id)]}
    ]

    result = run_tool_loop(
        request=wider_request,
        context=context,
        principal=principal(),
        policy=wider_policy,
        llm=ScriptedLLM([tool_turn("first"), unsupported_final]),
        tool=RecordingTool(),
        artifacts=reader,
        clock=lambda: NOW,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.MODEL_OUTPUT_INVALID


def test_invalid_tool_plan_is_rejected_before_any_tool_executes() -> None:
    context, reader = context_and_reader()
    llm = ScriptedLLM(
        [
            tool_turn("first")
            | {
                "tool_calls": [
                    tool_turn("first")["tool_calls"][0] | {"tool_name": "shell.exec"}  # type: ignore[index,operator]
                ]
            }
        ]
    )
    tool = RecordingTool()

    result = run_tool_loop(
        request=request(),
        context=context,
        principal=principal(),
        policy=policy(),
        llm=llm,
        tool=tool,
        artifacts=reader,
        clock=lambda: NOW,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CAPABILITY_DENIED
    assert tool.calls == []


def test_iteration_budget_hard_stops_a_non_final_loop() -> None:
    context, reader = context_and_reader()
    bounded = request(budget=request().budget.model_copy(update={"max_iterations": 1}))

    result = run_tool_loop(
        request=bounded,
        context=context,
        principal=principal(),
        policy=policy(),
        llm=ScriptedLLM([tool_turn("first")]),
        tool=RecordingTool(),
        artifacts=reader,
        clock=lambda: NOW,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.BUDGET_EXHAUSTED


def test_model_output_with_order_shaped_fields_fails_closed() -> None:
    context, reader = context_and_reader()

    result = run_tool_loop(
        request=request(),
        context=context,
        principal=principal(),
        policy=policy(),
        llm=ScriptedLLM([final_turn() | {"order": "buy"}]),
        tool=RecordingTool(),
        artifacts=reader,
        clock=lambda: NOW,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.MODEL_OUTPUT_INVALID


def test_tool_budget_is_reserved_before_batch_execution() -> None:
    context, reader = context_and_reader()
    bounded = request(budget=request().budget.model_copy(update={"max_tool_calls": 1}))
    tool = RecordingTool()

    result = run_tool_loop(
        request=bounded,
        context=context,
        principal=principal(),
        policy=policy(),
        llm=ScriptedLLM([tool_turn("first", "second")]),
        tool=tool,
        artifacts=reader,
        clock=lambda: NOW,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.BUDGET_EXHAUSTED
    assert tool.calls == []


def test_request_scope_is_narrower_than_reusable_policy_scope() -> None:
    context, reader = context_and_reader()
    wider_policy = policy().model_copy(
        update={"allowed_instrument_ids": frozenset({INSTRUMENT, OTHER_INSTRUMENT})}
    )
    escaped = tool_turn("first")
    escaped_call = escaped["tool_calls"][0]  # type: ignore[index]
    assert isinstance(escaped_call, dict)
    escaped_call["instrument_ids"] = [OTHER_INSTRUMENT]
    tool = RecordingTool()

    result = run_tool_loop(
        request=request(),
        context=context,
        principal=principal(),
        policy=wider_policy,
        llm=ScriptedLLM([escaped]),
        tool=tool,
        artifacts=reader,
        clock=lambda: NOW,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CAPABILITY_DENIED
    assert tool.calls == []


def test_deadline_is_rechecked_after_llm_completion() -> None:
    context, reader = context_and_reader()
    current = [NOW]
    scripted = ScriptedLLM([final_turn()])

    class AdvancingLLM:
        def complete(self, llm_request: object) -> object:
            result = scripted.complete(llm_request)  # type: ignore[arg-type]
            current[0] = NOW + timedelta(minutes=2)
            return result

    result = run_tool_loop(
        request=request(),
        context=context,
        principal=principal(),
        policy=policy(),
        llm=AdvancingLLM(),  # type: ignore[arg-type]
        tool=RecordingTool(),
        artifacts=reader,
        clock=lambda: current[0],
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.DEADLINE_EXCEEDED


def test_read_only_tool_batch_starts_in_parallel() -> None:
    context, reader = context_and_reader()
    barrier = Barrier(2, timeout=2)

    class BarrierTool(RecordingTool):
        def execute(self, call: object) -> object:
            barrier.wait()
            return super().execute(call)  # type: ignore[arg-type]

    result = run_tool_loop(
        request=request(),
        context=context,
        principal=principal(),
        policy=policy(),
        llm=ScriptedLLM([tool_turn("first", "second"), final_turn()]),
        tool=BarrierTool(),  # type: ignore[arg-type]
        artifacts=reader,
        clock=lambda: NOW,
    )

    assert isinstance(result, Success)


def test_per_call_timeout_fails_without_waiting_for_slow_tool() -> None:
    context, reader = context_and_reader()
    turn = tool_turn("first")
    calls = turn["tool_calls"]
    assert isinstance(calls, list)
    calls[0]["timeout_ms"] = 20

    class SlowTool(RecordingTool):
        def execute(self, call: object) -> object:
            sleep(0.25)
            return super().execute(call)  # type: ignore[arg-type]

    started = monotonic()
    result = run_tool_loop(
        request=request(),
        context=context,
        principal=principal(),
        policy=policy(),
        llm=ScriptedLLM([turn]),
        tool=SlowTool(),  # type: ignore[arg-type]
        artifacts=reader,
        clock=lambda: NOW,
    )
    elapsed = monotonic() - started

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.TOOL_FAILED
    assert elapsed < 0.15


def test_llm_outage_and_untyped_exception_fail_as_structured_results() -> None:
    context, reader = context_and_reader()

    class ExplodingLLM:
        def complete(self, llm_request: object) -> object:
            raise RuntimeError("provider secret must not escape")

    for llm in (FailingLLM(), ExplodingLLM()):
        result = run_tool_loop(
            request=request(),
            context=context,
            principal=principal(),
            policy=policy(),
            llm=llm,  # type: ignore[arg-type]
            tool=RecordingTool(),
            artifacts=reader,
            clock=lambda: NOW,
        )

        assert isinstance(result, Failure)
        assert "secret" not in result.error.message
