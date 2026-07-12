"""Bounded structured-output planning and parallel read-only tool loop."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stonks_agent.application.research.context_builder import (
    MAX_CONTEXT_BLOCKS,
    MAX_CONTEXT_TOTAL_BYTES,
    ResearchContext,
    load_untrusted_artifact,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.research import (
    LLMMessage,
    LLMRole,
    ResearchRequest,
    StructuredLLMRequest,
    StructuredLLMResponse,
    UntrustedContentBlock,
)
from stonks_agent.domain.tool_policy import (
    AuthorizedToolCall,
    ResearchPrincipal,
    ToolCall,
    ToolPolicy,
    ToolResult,
    authorize_tool_call,
    validate_tool_result,
)
from stonks_agent.domain.usage_budget import UsageConsumption, consume_usage
from stonks_agent.ports.artifact_store import ArtifactReaderPort
from stonks_agent.ports.llm import LLMPort
from stonks_agent.ports.tool import ToolPort
from stonks_contracts.common import ArtifactRef, UnitDecimal, stable_payload_hash


class ResearchAction(StrEnum):
    TOOLS = "tools"
    FINAL = "final"


class PlannedToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    arguments: dict[str, object]
    instrument_ids: frozenset[str]
    evidence_ids: frozenset[UUID]
    timeout_ms: int = Field(ge=1, le=120_000)
    output_limit_bytes: int = Field(ge=1, le=16_777_216)


class DraftClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=4_000)
    evidence_ids: frozenset[UUID] = Field(default_factory=frozenset, max_length=128)


class ResearchTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: ResearchAction
    tool_calls: tuple[PlannedToolCall, ...] = Field(
        default_factory=tuple, max_length=16
    )
    claims: tuple[DraftClaim, ...] = Field(default_factory=tuple, max_length=256)
    confidence: UnitDecimal | None = None
    counterarguments: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    risks: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    warnings: tuple[str, ...] = Field(default_factory=tuple, max_length=64)

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if self.action is ResearchAction.TOOLS:
            if not self.tool_calls or self.claims or self.confidence is not None:
                raise ValueError("tool turn must contain only tool calls")
        elif self.tool_calls or not self.claims or self.confidence is None:
            raise ValueError("final turn requires claims and confidence without tools")
        return self


class ResearchLoopResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    draft: ResearchTurn
    raw_output_artifact_ref: ArtifactRef
    tool_output_artifact_refs: tuple[ArtifactRef, ...]
    model_versions: tuple[str, ...]
    tool_versions: tuple[str, ...]
    usage: UsageConsumption


@dataclass(frozen=True, slots=True)
class _LoopState:
    usage: UsageConsumption
    blocks: tuple[UntrustedContentBlock, ...]
    total_bytes: int
    tool_refs: tuple[str, ...] = ()
    model_versions: tuple[str, ...] = ()
    tool_versions: tuple[str, ...] = ()


def run_tool_loop(
    *,
    request: ResearchRequest,
    context: ResearchContext,
    principal: ResearchPrincipal,
    policy: ToolPolicy,
    llm: LLMPort,
    tool: ToolPort,
    artifacts: ArtifactReaderPort,
    clock: Callable[[], datetime],
    max_parallel_tools: int = 8,
) -> Result[ResearchLoopResult]:
    state = _LoopState(
        usage=UsageConsumption(),
        blocks=context.blocks,
        total_bytes=context.total_bytes,
    )
    for iteration in range(request.budget.max_iterations):
        step = _run_iteration(
            request=request,
            state=state,
            principal=principal,
            policy=policy,
            llm=llm,
            tool=tool,
            artifacts=artifacts,
            clock=clock,
            iteration=iteration,
            max_parallel_tools=max_parallel_tools,
        )
        if isinstance(step, Failure):
            return step
        if isinstance(step.value, ResearchLoopResult):
            return Success(step.value)
        state = step.value
    return Failure(
        StructuredError(
            code=ErrorCode.BUDGET_EXHAUSTED,
            message="Research loop exhausted its iteration budget",
            details={"exceeded": ("iterations",)},
        )
    )


def _run_iteration(
    *,
    request: ResearchRequest,
    state: _LoopState,
    principal: ResearchPrincipal,
    policy: ToolPolicy,
    llm: LLMPort,
    tool: ToolPort,
    artifacts: ArtifactReaderPort,
    clock: Callable[[], datetime],
    iteration: int,
    max_parallel_tools: int,
) -> Result[_LoopState | ResearchLoopResult]:
    deadline = _check_deadline(request, clock())
    if deadline is not None:
        return deadline
    completed = _complete_turn(request, state.blocks, llm, iteration)
    if isinstance(completed, Failure):
        return completed
    response, turn = completed.value
    deadline = _check_deadline(request, clock())
    if deadline is not None:
        return deadline
    consumed = _consume_model_usage(request, state.usage, response.usage)
    if isinstance(consumed, Failure):
        return consumed
    state = _with_model_usage(state, response.model, consumed.value)
    if turn.action is ResearchAction.FINAL:
        return Success(_final_result(state, response, turn))
    return _run_tools(
        request,
        state,
        turn,
        principal,
        policy,
        tool,
        artifacts,
        clock,
        iteration,
        max_parallel_tools,
    )


def _run_tools(
    request: ResearchRequest,
    state: _LoopState,
    turn: ResearchTurn,
    principal: ResearchPrincipal,
    policy: ToolPolicy,
    tool: ToolPort,
    artifacts: ArtifactReaderPort,
    clock: Callable[[], datetime],
    iteration: int,
    max_parallel_tools: int,
) -> Result[_LoopState]:
    authorized = _authorize_batch(request, principal, policy, turn, iteration)
    if isinstance(authorized, Failure):
        return authorized
    reserved = consume_usage(
        request.budget,
        state.usage,
        UsageConsumption(tool_calls=len(authorized.value)),
    )
    if isinstance(reserved, Failure):
        return reserved
    executed = _execute_batch(tool, authorized.value, max_parallel_tools)
    if isinstance(executed, Failure):
        return executed
    elapsed = UsageConsumption(
        elapsed_ms=max(result.latency_ms for result in executed.value)
    )
    consumed = consume_usage(request.budget, reserved.value, elapsed)
    if isinstance(consumed, Failure):
        return consumed
    extended = _extend_context(
        state.blocks,
        state.total_bytes,
        executed.value,
        authorized.value,
        artifacts,
    )
    if isinstance(extended, Failure):
        return extended
    deadline = _check_deadline(request, clock())
    if deadline is not None:
        return deadline
    blocks, total_bytes = extended.value
    return Success(
        _LoopState(
            usage=consumed.value,
            blocks=blocks,
            total_bytes=total_bytes,
            tool_refs=(
                *state.tool_refs,
                *(result.artifact_ref for result in executed.value),
            ),
            model_versions=state.model_versions,
            tool_versions=(
                *state.tool_versions,
                *(result.tool_version for result in executed.value),
            ),
        )
    )


def _with_model_usage(
    state: _LoopState,
    model: str,
    usage: UsageConsumption,
) -> _LoopState:
    return _LoopState(
        usage=usage,
        blocks=state.blocks,
        total_bytes=state.total_bytes,
        tool_refs=state.tool_refs,
        model_versions=tuple(dict.fromkeys((*state.model_versions, model))),
        tool_versions=state.tool_versions,
    )


def _final_result(
    state: _LoopState,
    response: StructuredLLMResponse,
    turn: ResearchTurn,
) -> ResearchLoopResult:
    return ResearchLoopResult(
        draft=turn,
        raw_output_artifact_ref=response.raw_output_artifact_ref,
        tool_output_artifact_refs=state.tool_refs,
        model_versions=state.model_versions,
        tool_versions=tuple(dict.fromkeys(state.tool_versions)),
        usage=state.usage,
    )


def _complete_turn(
    request: ResearchRequest,
    blocks: tuple[UntrustedContentBlock, ...],
    llm: LLMPort,
    iteration: int,
) -> Result[tuple[StructuredLLMResponse, ResearchTurn]]:
    llm_request = StructuredLLMRequest(
        request_id=uuid5(request.request_id, f"research-turn:{iteration}"),
        model=f"policy:{request.model_policy_id}",
        messages=(
            LLMMessage(
                role=LLMRole.SYSTEM,
                content="Return only the requested research JSON schema.",
            ),
            LLMMessage(role=LLMRole.USER, content=request.question),
        ),
        untrusted_blocks=blocks,
        output_schema_name="research_turn",
        output_schema_version="1.0.0",
        output_schema=_TURN_SCHEMA,
        max_output_tokens=request.budget.max_output_tokens,
        deadline_at=request.deadline_at,
    )
    try:
        completed = llm.complete(llm_request)
    except Exception:
        return Failure(
            StructuredError(
                code=ErrorCode.DATA_UNAVAILABLE,
                message="Research model provider failed",
            )
        )
    if isinstance(completed, Failure):
        return completed
    response = completed.value
    if response.request_id != llm_request.request_id:
        return _invalid_model_output("request_identity_mismatch")
    try:
        turn = ResearchTurn.model_validate(response.parsed_output)
    except ValidationError:
        return _invalid_model_output("schema_validation_failed")
    return Success((response, turn))


def _consume_model_usage(
    request: ResearchRequest,
    current: UsageConsumption,
    reported: UsageConsumption,
) -> Result[UsageConsumption]:
    delta = UsageConsumption(
        iterations=1,
        input_tokens=reported.input_tokens,
        output_tokens=reported.output_tokens,
        cost_usd=reported.cost_usd,
        elapsed_ms=reported.elapsed_ms,
    )
    return consume_usage(request.budget, current, delta)


def _authorize_batch(
    request: ResearchRequest,
    principal: ResearchPrincipal,
    policy: ToolPolicy,
    turn: ResearchTurn,
    iteration: int,
) -> Result[tuple[AuthorizedToolCall, ...]]:
    if policy.policy_id != request.tool_policy_id:
        return _scope_denied("request_policy_mismatch")
    authorized: list[AuthorizedToolCall] = []
    for index, draft in enumerate(turn.tool_calls):
        if not draft.instrument_ids <= request.instrument_ids:
            return _scope_denied("request_instrument_scope_exceeded")
        if not draft.evidence_ids <= request.allowed_evidence_ids:
            return _scope_denied("request_evidence_scope_exceeded")
        call = ToolCall(
            call_id=uuid5(
                request.request_id,
                f"tool:{iteration}:{index}:{stable_payload_hash(draft)}",
            ),
            tool_name=draft.tool_name,
            arguments=draft.arguments,
            instrument_ids=draft.instrument_ids,
            evidence_ids=draft.evidence_ids,
            timeout_ms=draft.timeout_ms,
            output_limit_bytes=draft.output_limit_bytes,
        )
        decision = authorize_tool_call(policy, principal, call)
        if isinstance(decision, Failure):
            return decision
        authorized.append(decision.value)
    return Success(tuple(authorized))


def _execute_batch(
    tool: ToolPort,
    calls: tuple[AuthorizedToolCall, ...],
    max_parallel_tools: int,
) -> Result[tuple[ToolResult, ...]]:
    workers = max(1, min(max_parallel_tools, len(calls)))
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            outcomes = tuple(executor.map(tool.execute, calls))
    except Exception:
        return Failure(
            StructuredError(
                code=ErrorCode.TOOL_FAILED, message="Research tool execution failed"
            )
        )
    results: list[ToolResult] = []
    for call, outcome in zip(calls, outcomes, strict=True):
        if isinstance(outcome, Failure):
            return outcome
        validated = validate_tool_result(call, outcome.value)
        if isinstance(validated, Failure):
            return validated
        results.append(validated.value)
    return Success(tuple(results))


def _extend_context(
    blocks: tuple[UntrustedContentBlock, ...],
    total_bytes: int,
    results: tuple[ToolResult, ...],
    calls: tuple[AuthorizedToolCall, ...],
    artifacts: ArtifactReaderPort,
) -> Result[tuple[tuple[UntrustedContentBlock, ...], int]]:
    additions: list[UntrustedContentBlock] = []
    for call, result in zip(calls, results, strict=True):
        loaded = load_untrusted_artifact(
            result.artifact_ref,
            artifacts,
            max_bytes=min(call.output_limit_bytes, 32_768),
        )
        if isinstance(loaded, Failure):
            return loaded
        additions.append(loaded.value)
        total_bytes += len(loaded.value.content.encode("utf-8"))
    if (
        len(blocks) + len(additions) > MAX_CONTEXT_BLOCKS
        or total_bytes > MAX_CONTEXT_TOTAL_BYTES
    ):
        return Failure(
            StructuredError(
                code=ErrorCode.PAYLOAD_TOO_LARGE,
                message="Research tool context exceeded its bounded size",
            )
        )
    return Success(((*blocks, *additions), total_bytes))


def _check_deadline(request: ResearchRequest, now: datetime) -> Failure | None:
    if now.tzinfo is None or now > request.deadline_at:
        return Failure(
            StructuredError(
                code=ErrorCode.DEADLINE_EXCEEDED,
                message="Research deadline exceeded",
            )
        )
    return None


def _invalid_model_output(reason: str) -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.MODEL_OUTPUT_INVALID,
            message="Structured research model output is invalid",
            details={"reason": reason},
        )
    )


def _scope_denied(reason: str) -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.CAPABILITY_DENIED,
            message="Research tool call exceeded request scope",
            details={"reason": reason},
        )
    )


_TURN_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "tool_calls", "claims"],
    "properties": {
        "action": {"enum": ["tools", "final"]},
        "tool_calls": {"type": "array", "maxItems": 16},
        "claims": {"type": "array", "maxItems": 256},
        "confidence": {"type": ["string", "null"]},
        "counterarguments": {"type": "array", "maxItems": 64},
        "risks": {"type": "array", "maxItems": 64},
        "warnings": {"type": "array", "maxItems": 64},
    },
}
