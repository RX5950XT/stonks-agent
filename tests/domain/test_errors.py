from __future__ import annotations

import pytest

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    StructuredError,
    Success,
)
from stonks_agent.domain.redaction import REDACTED


def test_structured_error_is_immutable_and_machine_readable() -> None:
    error = StructuredError(
        code=ErrorCode.INVALID_INPUT,
        message="Invalid request",
        details={"field": "execution_mode"},
    )

    assert error.code is ErrorCode.INVALID_INPUT
    assert error.details == {"field": "execution_mode"}
    with pytest.raises(TypeError):
        error.details["field"] = "other"  # type: ignore[index]


def test_result_has_explicit_success_and_failure_variants() -> None:
    success = Success(value="artifact-1")
    failure = Failure(
        error=StructuredError(
            code=ErrorCode.NOT_FOUND,
            message="Artifact was not found",
        )
    )

    assert success.value == "artifact-1"
    assert failure.error.code is ErrorCode.NOT_FOUND


def test_structured_error_sanitizes_message_and_nested_details_at_construction() -> (
    None
):
    error = StructuredError(
        code=ErrorCode.INTERNAL_ERROR,
        message="provider failed with Bearer opaque-token-value",
        details={
            "nested": {"api_key": "sk-proj-sensitive-value"},
            "symbol": "AAPL",
        },
    )

    rendered = error.message + repr(dict(error.details))
    assert "opaque-token-value" not in rendered
    assert "sk-proj-sensitive-value" not in rendered
    assert error.details["nested"] == {"api_key": REDACTED}
    assert error.details["symbol"] == "AAPL"


def test_structured_error_remains_public_safe_when_details_exceed_redaction_bounds() -> (
    None
):
    error = StructuredError(
        code=ErrorCode.INTERNAL_ERROR,
        message="provider failure",
        details={str(index): index for index in range(10_001)},
    )

    assert error.details == {"redaction": "[TRUNCATED]"}
