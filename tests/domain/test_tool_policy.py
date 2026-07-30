from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import ValidationError

from stonks_agent.domain.capabilities import Capability
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.redaction import REDACTED
from stonks_agent.domain.tool_policy import (
    ResearchPrincipal,
    ToolArgumentKind,
    ToolArgumentSpec,
    ToolCall,
    ToolMutationClass,
    ToolPolicy,
    ToolResult,
    ToolRule,
    authorize_tool_call,
    validate_tool_result,
)

INSTRUMENT = "instrument:00000000-0000-4000-8000-000000000001"
OTHER_INSTRUMENT = "instrument:00000000-0000-4000-8000-000000000002"
EVIDENCE = UUID("00000000-0000-4000-8000-000000000003")


def rule(
    *,
    mutation_class: ToolMutationClass = ToolMutationClass.READ_ONLY,
) -> ToolRule:
    return ToolRule(
        name="evidence.lookup",
        mutation_class=mutation_class,
        arguments=(
            ToolArgumentSpec(
                name="query",
                kind=ToolArgumentKind.STRING,
                required=True,
                max_length=128,
            ),
            ToolArgumentSpec(
                name="limit",
                kind=ToolArgumentKind.INTEGER,
                required=False,
            ),
            ToolArgumentSpec(
                name="session_hint",
                kind=ToolArgumentKind.STRING,
                required=False,
                max_length=256,
                redact_in_audit=True,
            ),
        ),
        max_timeout_ms=2_000,
        max_output_bytes=4_096,
        audit_enabled=True,
        requires_instrument_scope=True,
        requires_evidence_scope=True,
    )


def policy() -> ToolPolicy:
    return ToolPolicy(
        policy_id="research-tools-v1",
        principal_profile="research-worker",
        tools=(rule(),),
        allowed_instrument_ids=frozenset({INSTRUMENT}),
        allowed_evidence_ids=frozenset({EVIDENCE}),
    )


def principal(**overrides: object) -> ResearchPrincipal:
    values: dict[str, object] = {
        "subject": "research-worker-1",
        "profile": "research-worker",
        "tool_policy_id": "research-tools-v1",
    }
    values.update(overrides)
    return ResearchPrincipal.model_validate(values)


def call(**overrides: object) -> ToolCall:
    values: dict[str, object] = {
        "call_id": uuid4(),
        "tool_name": "evidence.lookup",
        "arguments": {"query": "AAPL filing", "limit": 5, "session_hint": "secret"},
        "instrument_ids": frozenset({INSTRUMENT}),
        "evidence_ids": frozenset({EVIDENCE}),
        "timeout_ms": 1_000,
        "output_limit_bytes": 2_048,
    }
    values.update(overrides)
    return ToolCall.model_validate(values)


def test_allowlisted_read_only_call_returns_a_scoped_auditable_grant() -> None:
    result = authorize_tool_call(policy(), principal(), call())

    assert isinstance(result, Success)
    assert result.value.policy_id == "research-tools-v1"
    assert result.value.arguments["query"] == "AAPL filing"
    assert result.value.principal_subject == "research-worker-1"
    assert result.value.audit_arguments["session_hint"] == REDACTED
    assert result.value.audit_required is True


def test_unknown_tool_and_scope_escape_fail_closed() -> None:
    attempts = (
        call(tool_name="shell.exec"),
        call(instrument_ids=frozenset({OTHER_INSTRUMENT})),
        call(evidence_ids=frozenset({uuid4()})),
    )

    for attempt in attempts:
        result = authorize_tool_call(policy(), principal(), attempt)
        assert isinstance(result, Failure)
        assert result.error.code is ErrorCode.CAPABILITY_DENIED


def test_typed_arguments_reject_unknown_missing_and_bool_as_integer() -> None:
    attempts = (
        call(arguments={"limit": 5}),
        call(arguments={"query": "AAPL", "unexpected": True}),
        call(arguments={"query": "AAPL", "limit": True}),
        call(arguments={"query": "x" * 129}),
    )

    for attempt in attempts:
        result = authorize_tool_call(policy(), principal(), attempt)
        assert isinstance(result, Failure)
        assert result.error.code is ErrorCode.INVALID_INPUT


def test_timeout_and_output_limit_cannot_exceed_tool_rule() -> None:
    for attempt in (
        call(timeout_ms=2_001),
        call(output_limit_bytes=4_097),
    ):
        result = authorize_tool_call(policy(), principal(), attempt)
        assert isinstance(result, Failure)
        assert result.error.code is ErrorCode.CAPABILITY_DENIED


def test_required_scope_and_principal_policy_binding_cannot_be_bypassed() -> None:
    attempts = (
        (principal(), call(instrument_ids=frozenset())),
        (principal(), call(evidence_ids=frozenset())),
        (principal(tool_policy_id="another-policy"), call()),
        (principal(profile="another-profile"), call()),
    )

    for caller, attempt in attempts:
        result = authorize_tool_call(policy(), caller, attempt)
        assert isinstance(result, Failure)
        assert result.error.code is ErrorCode.CAPABILITY_DENIED


def test_research_policy_rejects_mutation_tools_and_unaudited_tools() -> None:
    for unsafe_rule in (
        rule(mutation_class=ToolMutationClass.EXECUTION),
        rule().model_copy(update={"audit_enabled": False}),
    ):
        try:
            ToolPolicy(
                policy_id="unsafe",
                principal_profile="research-worker",
                tools=(unsafe_rule,),
            )
        except ValidationError:
            pass
        else:  # pragma: no cover - security invariant
            raise AssertionError("unsafe research tool policy was accepted")


def test_research_principal_cannot_receive_mutation_capabilities() -> None:
    forbidden = (
        Capability.NETWORK_EGRESS,
        Capability.FILESYSTEM_WRITE,
        Capability.PROCESS_SPAWN,
        Capability.SECRET_READ,
        Capability.QUEUE_MUTATION,
        Capability.EXECUTION,
    )

    for capability in forbidden:
        try:
            ResearchPrincipal(
                subject="research-worker",
                profile="research-worker",
                tool_policy_id="research-tools-v1",
                allowed_capabilities=frozenset({capability}),
            )
        except ValidationError:
            pass
        else:  # pragma: no cover - security invariant
            raise AssertionError(f"unsafe capability accepted: {capability}")


def test_authorizer_rechecks_read_only_and_audit_invariants() -> None:
    for unsafe_rule in (
        rule().model_copy(update={"mutation_class": ToolMutationClass.EXECUTION}),
        rule().model_copy(update={"audit_enabled": False}),
    ):
        unsafe = policy().model_copy(update={"tools": (unsafe_rule,)})
        result = authorize_tool_call(unsafe, principal(), call())

        assert isinstance(result, Failure)
        assert result.error.code is ErrorCode.CAPABILITY_DENIED


def test_tool_result_must_match_call_identity_hash_and_output_limit() -> None:
    authorized = authorize_tool_call(policy(), principal(), call())
    assert isinstance(authorized, Success)
    valid = {
        "call_id": authorized.value.call_id,
        "artifact_ref": f"sha256:{'a' * 64}",
        "content_hash": "a" * 64,
        "content_type": "application/json",
        "byte_count": 2_048,
        "tool_version": "fixture/1",
        "materialized_evidence_ids": [str(EVIDENCE)],
        "observed_at": datetime(2026, 7, 12, tzinfo=UTC),
    }

    accepted = validate_tool_result(
        authorized.value,
        ToolResult.model_validate(valid),
    )
    assert isinstance(accepted, Success)

    invalid_results = (
        ToolResult.model_validate(valid | {"call_id": uuid4()}),
        ToolResult.model_validate(valid | {"artifact_ref": f"sha256:{'b' * 64}"}),
        ToolResult.model_validate(valid | {"byte_count": 2_049}),
        ToolResult.model_validate(
            valid | {"materialized_evidence_ids": [str(uuid4())]}
        ),
    )
    for result in invalid_results:
        rejected = validate_tool_result(authorized.value, result)
        assert isinstance(rejected, Failure)


def test_non_finite_numeric_argument_is_rejected() -> None:
    numeric_rule = rule().model_copy(
        update={
            "arguments": (
                ToolArgumentSpec(
                    name="score",
                    kind=ToolArgumentKind.NUMBER,
                    required=True,
                ),
            )
        }
    )
    numeric_policy = policy().model_copy(update={"tools": (numeric_rule,)})

    for value in (float("nan"), float("inf"), float("-inf")):
        result = authorize_tool_call(
            numeric_policy,
            principal(),
            call(arguments={"score": value}),
        )
        assert isinstance(result, Failure)
        assert result.error.code is ErrorCode.INVALID_INPUT
