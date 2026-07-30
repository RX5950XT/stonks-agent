"""Shared same-origin intent proof for loopback GUI configuration/workflow writes."""

from __future__ import annotations

import hmac

from starlette.requests import Request

from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError

GUI_INTENT_HEADER = "X-Stonks-Intent"


def validate_gui_mutation(
    request: Request,
    intent_token: str,
    *,
    query_error: str,
) -> Failure | None:
    if tuple(request.query_params):
        return _failure(ErrorCode.INVALID_INPUT, query_error)
    content_types = request.headers.getlist("content-type")
    origins = request.headers.getlist("origin")
    intents = request.headers.getlist(GUI_INTENT_HEADER)
    hosts = request.headers.getlist("host")
    expected_origin = f"{request.url.scheme}://{hosts[0]}" if len(hosts) == 1 else None
    if content_types != ["application/json"]:
        return _failure(ErrorCode.INVALID_INPUT, "GUI request content type is invalid")
    if len(origins) != 1 or expected_origin is None or origins[0] != expected_origin:
        return _failure(ErrorCode.FORBIDDEN, "GUI request origin is not allowed")
    if len(intents) != 1 or not hmac.compare_digest(intents[0], intent_token):
        return _failure(ErrorCode.FORBIDDEN, "GUI request intent is invalid")
    fetch_sites = request.headers.getlist("sec-fetch-site")
    if fetch_sites and fetch_sites != ["same-origin"]:
        return _failure(ErrorCode.FORBIDDEN, "GUI request origin is not allowed")
    return None


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
