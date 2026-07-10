from __future__ import annotations

import pytest

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    StructuredError,
    Success,
)


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
