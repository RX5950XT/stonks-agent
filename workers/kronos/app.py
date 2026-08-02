"""Minimal bounded HTTP surface for the isolated Kronos runtime."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from stonks_contracts.kronos import KronosWorkerRequest
from stonks_service_auth import (
    RequestBodyReadError,
    ServiceAccessTarget,
    ServiceAdmissionMiddleware,
    ServiceAuthenticator,
    ServicePermission,
    ServiceReceiver,
    ServiceResourceKind,
    authorize_service_dispatch,
    exactly_one_authorization_header,
    invalid_or_oversized_content_length,
    read_bounded_request_body,
)
from workers.kronos.adapter import KronosPreflightRequest, KronosWorker


def create_app(
    *,
    worker: KronosWorker,
    authenticator: ServiceAuthenticator,
    max_request_bytes: int = 65_536,
    max_concurrency: int = 1,
) -> FastAPI:
    if not 1 <= max_request_bytes <= 1_048_576:
        raise ValueError("max_request_bytes is outside the supported range")
    if max_concurrency != 1:
        raise ValueError("max_concurrency must be exactly one")
    capacity = asyncio.Semaphore(max_concurrency)
    app = FastAPI(
        title="Stonks Kronos Worker",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(ServiceAdmissionMiddleware)

    @app.get("/healthz")
    def health() -> JSONResponse:
        return _envelope(
            200,
            data={
                "worker_version": worker.policy.worker_version,
                "profile": worker.policy.profile.value,
            },
        )

    @app.get("/readyz")
    def ready() -> JSONResponse:
        status = 200 if worker.loader.ready else 503
        return _envelope(status, data={"ready": worker.loader.ready})

    @app.post("/v1/preflight")
    async def preflight(incoming: Request) -> JSONResponse:
        principal = authenticator.authenticate(
            exactly_one_authorization_header(incoming.scope["headers"])
        )
        if principal is None:
            return _authentication_error()
        rejection = _validate_headers(incoming, max_request_bytes)
        if rejection is not None:
            return rejection
        if not await _try_acquire(capacity):
            return _error(429, "worker_busy", "Worker is at capacity")
        try:
            try:
                body = await read_bounded_request_body(
                    incoming.receive,
                    max_bytes=max_request_bytes,
                )
            except RequestBodyReadError as error:
                return _error(error.status_code, error.code, error.safe_message)
            try:
                request = KronosPreflightRequest.model_validate_json(body)
            except (ValidationError, json.JSONDecodeError):
                return _error(400, "invalid_request", "Worker request is invalid")
            if not authorize_service_dispatch(
                principal,
                permission=ServicePermission.PREFLIGHT_ASSIGNED_RESEARCH,
                target=ServiceAccessTarget(
                    kind=ServiceResourceKind.JOB,
                    identifier=str(request.request_id),
                ),
                receiver=ServiceReceiver.KRONOS,
                attempt_generation=0,
                attempt_nonce="",
                request_payload=request.model_dump(mode="json"),
                deadline=None,
            ):
                return _error(403, "forbidden", "Service target access denied")
            outcome = worker.preflight(request)
            if outcome.error is not None:
                status = 503 if outcome.error.code == "model_not_ready" else 409
                return _error(status, outcome.error.code, outcome.error.message)
            assert outcome.value is not None
            return _envelope(200, data=outcome.value.model_dump(mode="json"))
        finally:
            capacity.release()

    @app.post("/v1/forecast")
    async def forecast(incoming: Request) -> JSONResponse:
        principal = authenticator.authenticate(
            exactly_one_authorization_header(incoming.scope["headers"])
        )
        if principal is None:
            return _authentication_error()
        rejection = _validate_headers(incoming, max_request_bytes)
        if rejection is not None:
            return rejection
        if not await _try_acquire(capacity):
            return _error(429, "worker_busy", "Worker is at capacity")
        try:
            try:
                body = await read_bounded_request_body(
                    incoming.receive,
                    max_bytes=max_request_bytes,
                )
            except RequestBodyReadError as error:
                return _error(error.status_code, error.code, error.safe_message)
            try:
                request = KronosWorkerRequest.model_validate_json(body)
            except (ValidationError, json.JSONDecodeError):
                return _error(400, "invalid_request", "Worker request is invalid")
            if not authorize_service_dispatch(
                principal,
                permission=ServicePermission.DISPATCH_ASSIGNED_RESEARCH,
                target=ServiceAccessTarget(
                    kind=ServiceResourceKind.JOB,
                    identifier=str(request.job_id),
                ),
                receiver=ServiceReceiver.KRONOS,
                attempt_generation=request.attempt_generation,
                attempt_nonce=request.attempt_nonce,
                request_payload=request.model_dump(mode="json"),
                deadline=request.deadline,
            ):
                return _error(403, "forbidden", "Service target access denied")
            outcome = await run_in_threadpool(worker.forecast, request)
        finally:
            capacity.release()
        if outcome.error is not None:
            status = _worker_error_status(outcome.error.code)
            return _error(status, outcome.error.code, outcome.error.message)
        assert outcome.value is not None
        return _envelope(200, data=outcome.value.model_dump(mode="json"))

    return app


async def _try_acquire(capacity: asyncio.Semaphore) -> bool:
    if capacity.locked():
        return False
    await capacity.acquire()
    return True


def _validate_headers(incoming: Request, maximum: int) -> JSONResponse | None:
    declared = incoming.headers.get("content-length")
    if invalid_or_oversized_content_length(declared, maximum):
        return _error(413, "request_too_large", "Worker request is too large")
    media_type = incoming.headers.get("content-type", "").split(";", 1)[0]
    if media_type != "application/json":
        return _error(415, "unsupported_media_type", "JSON request required")
    if incoming.headers.get("content-encoding", "identity").lower() != "identity":
        return _error(415, "unsupported_content_encoding", "Encoded body denied")
    return None


def _error(status: int, code: str, message: str) -> JSONResponse:
    return _envelope(status, error={"code": code, "message": message})


def _authentication_error() -> JSONResponse:
    response = _error(401, "unauthorized", "Service authentication failed")
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


def _worker_error_status(code: str) -> int:
    if code == "model_not_ready":
        return 503
    if code in {"runtime_mismatch", "deadline_expired"}:
        return 409
    if code in {"runtime_invalid", "inference_failed"}:
        return 503
    return 422


def _envelope(
    status: int,
    *,
    data: object | None = None,
    error: dict[str, str] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "success": error is None and status < 400,
        "status": status,
        "data": data,
        "error": error,
        "metadata": None,
    }
    return JSONResponse(status_code=status, content=payload)
