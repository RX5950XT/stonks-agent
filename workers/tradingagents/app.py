"""Minimal HTTP surface for the isolated TradingAgents worker."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from stonks_service_auth import (
    ServiceAccessTarget,
    ServiceAuthenticator,
    ServicePermission,
    ServiceReceiver,
    ServiceResourceKind,
    authorize_service_dispatch,
    exactly_one_authorization_header,
    invalid_or_oversized_content_length,
    service_auth_source_hash,
)
from workers.tradingagents.adapter import (
    TradingAgentsRequest,
    TradingAgentsWorker,
    WorkerFailure,
)


def create_app(
    *,
    worker: TradingAgentsWorker,
    authenticator: ServiceAuthenticator,
    max_request_bytes: int = 1_048_576,
) -> FastAPI:
    if not 1 <= max_request_bytes <= 16_777_216:
        raise ValueError("max_request_bytes is outside the supported range")
    app = FastAPI(
        title="Stonks TradingAgents Worker",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz")
    def health() -> JSONResponse:
        return _envelope(
            200,
            data={
                "worker_version": worker.policy.worker_version,
                "upstream_commit": worker.policy.upstream_commit,
                "profile": worker.policy.profile.value,
                "service_auth_source_hash": service_auth_source_hash(),
            },
        )

    @app.post("/v1/analyze")
    async def analyze(incoming: Request) -> JSONResponse:
        principal = authenticator.authenticate(
            exactly_one_authorization_header(incoming.scope["headers"])
        )
        if principal is None:
            return _authentication_error()
        declared = incoming.headers.get("content-length")
        if invalid_or_oversized_content_length(declared, max_request_bytes):
            return _error(413, "request_too_large", "Worker request is too large")
        if (
            incoming.headers.get("content-type", "").split(";", 1)[0]
            != "application/json"
        ):
            return _error(415, "unsupported_media_type", "JSON request required")
        encoding = incoming.headers.get("content-encoding", "identity").lower()
        if encoding != "identity":
            return _error(415, "unsupported_content_encoding", "Encoded body denied")
        body = await _read_bounded(incoming, max_request_bytes)
        if body is None:
            return _error(413, "request_too_large", "Worker request is too large")
        try:
            request = TradingAgentsRequest.model_validate_json(body)
        except (ValidationError, json.JSONDecodeError):
            return _error(400, "invalid_request", "Worker request is invalid")
        if not authorize_service_dispatch(
            principal,
            permission=ServicePermission.DISPATCH_ASSIGNED_RESEARCH,
            target=ServiceAccessTarget(
                kind=ServiceResourceKind.JOB,
                identifier=str(request.job_id),
            ),
            receiver=ServiceReceiver.TRADINGAGENTS,
            attempt_generation=request.attempt_generation,
            attempt_nonce=request.attempt_nonce,
            request_payload=request.model_dump(mode="json"),
            deadline=request.deadline,
        ):
            return _error(403, "forbidden", "Service target access denied")
        result = worker.analyze(request)
        if isinstance(result, WorkerFailure):
            return _error(
                _status_for(result.error.code), result.error.code, result.error.message
            )
        return _envelope(200, data=result.value.model_dump(mode="json"))

    return app


async def _read_bounded(incoming: Request, maximum: int) -> bytes | None:
    body = bytearray()
    async for chunk in incoming.stream():
        if len(chunk) > maximum - len(body):
            return None
        body.extend(chunk)
    return bytes(body)


def _status_for(code: str) -> int:
    return {
        "profile_mismatch": 409,
        "deadline_exceeded": 408,
        "evidence_too_large": 413,
        "source_scope_exceeded": 422,
    }.get(code, 503)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return _envelope(status, error={"code": code, "message": message})


def _authentication_error() -> JSONResponse:
    response = _error(401, "unauthorized", "Service authentication failed")
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


def _envelope(
    status: int,
    *,
    data: object | None = None,
    error: dict[str, str] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "success": error is None,
        "status": status,
        "data": data,
        "error": error,
        "metadata": None,
    }
    return JSONResponse(status_code=status, content=payload)
