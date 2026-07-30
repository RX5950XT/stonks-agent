"""Bounded HTTP surface for the isolated quant-lab worker."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from stonks_contracts.quant_lab import QuantResearchJob
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
from workers.quant_lab.qlib_adapter import QuantLabWorker, WorkerFailure


def create_app(
    *,
    worker: QuantLabWorker,
    authenticator: ServiceAuthenticator,
    max_request_bytes: int = 16_777_216,
    max_concurrency: int = 1,
) -> FastAPI:
    if not 1 <= max_request_bytes <= 16_777_216:
        raise ValueError("max_request_bytes is outside the supported range")
    if max_concurrency != 1:
        raise ValueError("max_concurrency must be exactly one")
    capacity = asyncio.Semaphore(max_concurrency)
    app = FastAPI(
        title="Stonks Quant Lab Worker",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(ServiceAdmissionMiddleware)

    @app.get("/healthz")
    def health() -> JSONResponse:
        runtime = worker.policy.runtime
        return _envelope(
            200,
            data={
                "worker_version": runtime.worker_version,
                "qlib_commit": runtime.qlib_commit,
                "runtime_hash": runtime.runtime_hash,
            },
        )

    @app.post("/v1/research")
    async def research(incoming: Request) -> JSONResponse:
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
                job = QuantResearchJob.model_validate_json(body)
            except (ValidationError, json.JSONDecodeError):
                return _error(400, "invalid_request", "Worker request is invalid")
            if not authorize_service_dispatch(
                principal,
                permission=ServicePermission.DISPATCH_ASSIGNED_RESEARCH,
                target=ServiceAccessTarget(
                    kind=ServiceResourceKind.JOB,
                    identifier=str(job.job_id),
                ),
                receiver=ServiceReceiver.QUANT_LAB,
                attempt_generation=job.attempt_generation,
                attempt_nonce=job.attempt_nonce,
                request_payload=job.model_dump(mode="json"),
                deadline=job.deadline,
            ):
                return _error(403, "forbidden", "Service target access denied")
            outcome = await run_in_threadpool(worker.research, job)
        finally:
            capacity.release()
        if isinstance(outcome, WorkerFailure):
            return _error(
                _status_for(outcome.error.code),
                outcome.error.code,
                outcome.error.message,
            )
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


def _status_for(code: str) -> int:
    if code == "deadline_expired":
        return 408
    if code == "dataset_too_large":
        return 413
    if code in {"runtime_mismatch", "split_invalid"}:
        return 409
    return 503


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
        "success": error is None and status < 400,
        "status": status,
        "data": data,
        "error": error,
        "metadata": None,
    }
    return JSONResponse(status_code=status, content=payload)
