from __future__ import annotations

import pytest
from pydantic import ValidationError

from stonks_agent.domain.errors import ErrorCode, StructuredError
from stonks_agent.entrypoints.api.envelope import (
    ApiError,
    ErrorEnvelope,
    Pagination,
    ResponseMetadata,
    SuccessEnvelope,
    error_envelope,
    success_envelope,
    unexpected_error_envelope,
)


def test_success_envelope_has_uniform_shape_and_pagination_metadata() -> None:
    pagination = Pagination(page=2, page_size=10, total_items=21)
    response = success_envelope(
        data={"items": ["AAPL"]},
        status=200,
        metadata=ResponseMetadata(pagination=pagination),
    )

    payload = response.model_dump(mode="json")
    assert set(payload) == {"success", "status", "data", "error", "metadata"}
    assert payload["success"] is True
    assert payload["error"] is None
    assert payload["metadata"]["pagination"] == {
        "page": 2,
        "page_size": 10,
        "total_items": 21,
        "total_pages": 3,
    }


def test_domain_error_mapping_redacts_details_and_uses_http_status() -> None:
    error = StructuredError(
        code=ErrorCode.FORBIDDEN,
        message="Permission denied",
        details={"authorization": "Bearer top-secret"},
    )

    response = error_envelope(error)
    rendered = response.model_dump_json()

    assert response.status == 403
    assert response.error.code == "forbidden"
    assert "top-secret" not in rendered
    assert "Traceback" not in rendered


def test_unexpected_exception_never_exposes_message_stack_or_secret() -> None:
    exception = RuntimeError("Bearer top-secret\nTraceback: internal/path.py:12")

    response = unexpected_error_envelope(exception)
    rendered = response.model_dump_json()

    assert response.status == 500
    assert response.error.code == "internal_error"
    assert response.error.message == "Internal server error"
    assert "top-secret" not in rendered
    assert "Traceback" not in rendered


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (Pagination, {"page": 1, "page_size": 10, "total_items": 1, "extra": 1}),
        (ApiError, {"code": "bad", "message": "bad", "extra": 1}),
        (
            ResponseMetadata,
            {"pagination": None, "extra": 1},
        ),
    ],
)
def test_api_models_reject_unknown_external_fields(
    model: type, payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_envelope_status_ranges_are_validated() -> None:
    with pytest.raises(ValidationError):
        SuccessEnvelope(data={}, status=500)
    with pytest.raises(ValidationError):
        ErrorEnvelope(
            status=200,
            error=ApiError(code="invalid", message="invalid"),
        )
