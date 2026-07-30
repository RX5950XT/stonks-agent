"""Deny-by-default authorization for bounded read-only research tools."""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Self, assert_never
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stonks_agent.domain.capabilities import Capability
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.redaction import REDACTED, redact
from stonks_contracts.common import ArtifactRef, Sha256, UTCDateTime


class ToolArgumentKind(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    STRING_LIST = "string_list"


class ToolMutationClass(StrEnum):
    READ_ONLY = "read_only"
    FILESYSTEM_WRITE = "filesystem_write"
    PROCESS_SPAWN = "process_spawn"
    SECRET_READ = "secret_read"
    QUEUE_MUTATION = "queue_mutation"
    EXECUTION = "execution"


class ToolArgumentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    kind: ToolArgumentKind
    required: bool = True
    max_length: int | None = Field(default=None, ge=1, le=65_536)
    redact_in_audit: bool = False

    @model_validator(mode="after")
    def validate_length_semantics(self) -> Self:
        if self.max_length is not None and self.kind not in {
            ToolArgumentKind.STRING,
            ToolArgumentKind.STRING_LIST,
        }:
            raise ValueError("max_length applies only to string arguments")
        return self


class ToolRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    mutation_class: ToolMutationClass
    arguments: tuple[ToolArgumentSpec, ...] = Field(
        default_factory=tuple, max_length=32
    )
    max_timeout_ms: int = Field(ge=1, le=120_000)
    max_output_bytes: int = Field(ge=1, le=16_777_216)
    audit_enabled: bool = True
    requires_instrument_scope: bool = True
    requires_evidence_scope: bool = True

    @model_validator(mode="after")
    def validate_arguments(self) -> Self:
        names = tuple(argument.name for argument in self.arguments)
        if len(names) != len(set(names)):
            raise ValueError("tool argument names must be unique")
        return self


class ToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    principal_profile: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    tools: tuple[ToolRule, ...] = Field(default_factory=tuple, max_length=64)
    allowed_instrument_ids: frozenset[str] = Field(default_factory=frozenset)
    allowed_evidence_ids: frozenset[UUID] = Field(default_factory=frozenset)

    @field_validator("allowed_instrument_ids")
    @classmethod
    def validate_instrument_ids(cls, values: frozenset[str]) -> frozenset[str]:
        for value in values:
            if not value or len(value) > 128 or value.strip() != value:
                raise ValueError("instrument scope contains an invalid identifier")
        return values

    @model_validator(mode="after")
    def validate_research_tools(self) -> Self:
        names = tuple(tool.name for tool in self.tools)
        if len(names) != len(set(names)):
            raise ValueError("tool allowlist names must be unique")
        if any(
            tool.mutation_class is not ToolMutationClass.READ_ONLY
            for tool in self.tools
        ):
            raise ValueError("research tool policy permits read-only tools only")
        if any(not tool.audit_enabled for tool in self.tools):
            raise ValueError("research tool calls must be audited")
        return self


class ResearchPrincipal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(pattern=r"^[A-Za-z0-9_.:@-]{1,128}$")
    profile: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    tool_policy_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    allowed_capabilities: frozenset[Capability] = Field(default_factory=frozenset)

    @field_validator("allowed_capabilities")
    @classmethod
    def reject_mutation_capabilities(
        cls,
        capabilities: frozenset[Capability],
    ) -> frozenset[Capability]:
        if capabilities:
            raise ValueError("research principal cannot receive ambient capabilities")
        return capabilities


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: UUID
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    arguments: dict[str, object] = Field(default_factory=dict, max_length=32)
    instrument_ids: frozenset[str] = Field(default_factory=frozenset)
    evidence_ids: frozenset[UUID] = Field(default_factory=frozenset)
    timeout_ms: int = Field(ge=1, le=120_000)
    output_limit_bytes: int = Field(ge=1, le=16_777_216)


class AuthorizedToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: UUID
    policy_id: str
    principal_subject: str
    principal_profile: str
    tool_name: str
    arguments: dict[str, object]
    audit_arguments: dict[str, object]
    instrument_ids: frozenset[str]
    evidence_ids: frozenset[UUID]
    timeout_ms: int
    output_limit_bytes: int
    audit_required: bool = True


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: UUID
    artifact_ref: ArtifactRef
    content_hash: Sha256
    content_type: str = Field(min_length=1, max_length=128)
    byte_count: int = Field(ge=0, le=16_777_216)
    tool_version: str = Field(min_length=1, max_length=128)
    materialized_evidence_ids: frozenset[UUID] = Field(max_length=128)
    latency_ms: int = Field(default=0, ge=0, le=120_000)
    untrusted_content: bool = True
    observed_at: UTCDateTime

    @field_validator("untrusted_content")
    @classmethod
    def require_untrusted_marker(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("external tool output must remain untrusted")
        return value


def authorize_tool_call(
    policy: ToolPolicy,
    principal: ResearchPrincipal,
    call: ToolCall,
) -> Result[AuthorizedToolCall]:
    """Authorize one exact tool call without granting ambient capabilities."""

    if (
        principal.tool_policy_id != policy.policy_id
        or principal.profile != policy.principal_profile
    ):
        return _denied(call, "principal_policy_mismatch")
    rule = next(
        (candidate for candidate in policy.tools if candidate.name == call.tool_name),
        None,
    )
    if rule is None:
        return _denied(call, "tool_not_allowlisted")
    if rule.mutation_class is not ToolMutationClass.READ_ONLY or not rule.audit_enabled:
        return _denied(call, "unsafe_tool_rule")
    if not call.instrument_ids <= policy.allowed_instrument_ids:
        return _denied(call, "instrument_scope_exceeded")
    if not call.evidence_ids <= policy.allowed_evidence_ids:
        return _denied(call, "evidence_scope_exceeded")
    if rule.requires_instrument_scope and not call.instrument_ids:
        return _denied(call, "instrument_scope_required")
    if rule.requires_evidence_scope and not call.evidence_ids:
        return _denied(call, "evidence_scope_required")
    if call.timeout_ms > rule.max_timeout_ms:
        return _denied(call, "timeout_limit_exceeded")
    if call.output_limit_bytes > rule.max_output_bytes:
        return _denied(call, "output_limit_exceeded")
    argument_error = _validate_arguments(rule.arguments, call.arguments)
    if argument_error is not None:
        return Failure(
            StructuredError(
                code=ErrorCode.INVALID_INPUT,
                message="Tool arguments failed validation",
                details={"reason": argument_error, "tool": call.tool_name},
            )
        )
    audit_arguments = _audit_arguments(rule.arguments, call.arguments)
    return Success(
        AuthorizedToolCall(
            call_id=call.call_id,
            policy_id=policy.policy_id,
            principal_subject=principal.subject,
            principal_profile=principal.profile,
            tool_name=call.tool_name,
            arguments=dict(call.arguments),
            audit_arguments=audit_arguments,
            instrument_ids=call.instrument_ids,
            evidence_ids=call.evidence_ids,
            timeout_ms=call.timeout_ms,
            output_limit_bytes=call.output_limit_bytes,
            audit_required=True,
        )
    )


def validate_tool_result(
    call: AuthorizedToolCall,
    result: ToolResult,
) -> Result[ToolResult]:
    """Validate an untrusted adapter result against its exact authorization."""

    if result.call_id != call.call_id:
        return _invalid_result("call_identity_mismatch")
    if result.artifact_ref != f"sha256:{result.content_hash}":
        return _invalid_result("artifact_hash_mismatch")
    if result.byte_count > call.output_limit_bytes:
        return Failure(
            StructuredError(
                code=ErrorCode.PAYLOAD_TOO_LARGE,
                message="Research tool output exceeded authorized limit",
                details={"limit_bytes": call.output_limit_bytes},
            )
        )
    if not result.materialized_evidence_ids <= call.evidence_ids:
        return _invalid_result("materialized_evidence_scope_exceeded")
    return Success(result)


def _validate_arguments(
    specs: tuple[ToolArgumentSpec, ...],
    arguments: dict[str, object],
) -> str | None:
    expected = {spec.name: spec for spec in specs}
    unknown = set(arguments) - set(expected)
    if unknown:
        return "unknown_arguments"
    if any(spec.required and spec.name not in arguments for spec in specs):
        return "missing_required_arguments"
    for name, value in arguments.items():
        if not _matches_spec(expected[name], value):
            return f"invalid_argument:{name}"
    return None


def _matches_spec(spec: ToolArgumentSpec, value: object) -> bool:
    if spec.kind is ToolArgumentKind.STRING:
        return isinstance(value, str) and _valid_string(value, spec.max_length)
    if spec.kind is ToolArgumentKind.INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if spec.kind is ToolArgumentKind.NUMBER:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    if spec.kind is ToolArgumentKind.BOOLEAN:
        return isinstance(value, bool)
    if spec.kind is ToolArgumentKind.STRING_LIST:
        return (
            isinstance(value, (list, tuple))
            and len(value) <= 64
            and all(
                isinstance(item, str) and _valid_string(item, spec.max_length)
                for item in value
            )
        )
    assert_never(spec.kind)


def _valid_string(value: str, max_length: int | None) -> bool:
    return (
        value.strip() == value
        and not any(ord(character) < 32 for character in value)
        and (max_length is None or len(value) <= max_length)
    )


def _audit_arguments(
    specs: tuple[ToolArgumentSpec, ...],
    arguments: dict[str, object],
) -> dict[str, object]:
    redacted = redact(arguments)
    if not isinstance(redacted, dict):  # pragma: no cover - mapping invariant
        raise TypeError("redacted tool arguments must remain a dictionary")
    for spec in specs:
        if spec.redact_in_audit and spec.name in redacted:
            redacted[spec.name] = REDACTED
    return redacted


def _denied(call: ToolCall, reason: str) -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.CAPABILITY_DENIED,
            message="Research tool call denied",
            details={"reason": reason, "tool": call.tool_name},
        )
    )


def _invalid_result(reason: str) -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.INVALID_INPUT,
            message="Research tool result failed validation",
            details={"reason": reason},
        )
    )
