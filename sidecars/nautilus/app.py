"""Bounded HTTP surface for the optional NautilusTrader sidecar."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from sidecars.nautilus.adapter import NautilusAdapter, WorkerFailure
from stonks_contracts.backtest import BacktestJob
from stonks_service_auth import (
    ServiceAccessTarget,
    ServiceAuthenticator,
    ServicePermission,
    ServiceReceiver,
    ServiceResourceKind,
    authorize_service_dispatch,
    exactly_one_authorization_header,
)


def create_app(
    *,
    adapter: NautilusAdapter,
    authenticator: ServiceAuthenticator,
    max_request_bytes: int,
    max_concurrency: int = 1,
) -> FastAPI:
    if not 1 <= max_request_bytes <= 16_777_216:
        raise ValueError("max_request_bytes is outside the supported range")
    if not 1 <= max_concurrency <= 16:
        raise ValueError("max_concurrency is outside the supported range")
    capacity = asyncio.Semaphore(max_concurrency)
    app = FastAPI(
        title="Stonks Nautilus Backtest Sidecar",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz")
    def health() -> JSONResponse:
        runtime = adapter.policy.runtime
        return _envelope(
            200,
            data={
                "engine": runtime.engine.value,
                "engine_version": runtime.engine_version,
                "adapter_version": runtime.adapter_version,
                "runtime_hash": runtime.runtime_hash,
                "image_digest": runtime.image_digest,
            },
        )

    @app.post("/v1/backtests")
    async def backtest(incoming: Request) -> JSONResponse:
        principal = authenticator.authenticate(
            exactly_one_authorization_header(incoming.scope["headers"])
        )
        if principal is None:
            return _authentication_error()
        rejection = _validate_headers(incoming, max_request_bytes)
        if rejection is not None:
            return rejection
        body = await _read_bounded(incoming, max_request_bytes)
        if body is None:
            return _error(413, "request_too_large", "Backtest request is too large")
        try:
            job = BacktestJob.model_validate_json(body)
        except (ValidationError, json.JSONDecodeError):
            return _error(400, "invalid_request", "Backtest request is invalid")
        if not authorize_service_dispatch(
            principal,
            permission=ServicePermission.DISPATCH_ASSIGNED_BACKTEST,
            target=ServiceAccessTarget(
                kind=ServiceResourceKind.BACKTEST_JOB,
                identifier=str(job.job_id),
            ),
            receiver=ServiceReceiver.NAUTILUS,
            attempt_generation=job.attempt_generation,
            attempt_nonce=job.attempt_nonce,
            request_payload=job.model_dump(mode="json"),
            deadline=job.deadline,
        ):
            return _error(403, "forbidden", "Service target access denied")
        try:
            await asyncio.wait_for(capacity.acquire(), timeout=0.1)
        except TimeoutError:
            return _error(429, "worker_busy", "Nautilus worker is at capacity")
        try:
            outcome = await run_in_threadpool(adapter.run, job)
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


def _validate_headers(incoming: Request, maximum: int) -> JSONResponse | None:
    declared = incoming.headers.get("content-length")
    if declared is not None:
        maximum_text = str(maximum)
        invalid_length = (
            not declared.isdecimal()
            or len(declared) > len(maximum_text)
            or (len(declared) == len(maximum_text) and declared > maximum_text)
        )
        if invalid_length:
            return _error(413, "request_too_large", "Backtest request is too large")
    media_type = incoming.headers.get("content-type", "").split(";", 1)[0]
    if media_type != "application/json":
        return _error(415, "unsupported_media_type", "JSON request required")
    if incoming.headers.get("content-encoding", "identity").lower() != "identity":
        return _error(415, "unsupported_content_encoding", "Encoded body denied")
    return None


async def _read_bounded(incoming: Request, maximum: int) -> bytes | None:
    body = bytearray()
    async for chunk in incoming.stream():
        if len(chunk) > maximum - len(body):
            return None
        body.extend(chunk)
    return bytes(body)


def _status_for(code: str) -> int:
    if code in {"deadline_expired", "job_not_ready"}:
        return 408
    if code == "job_too_large":
        return 413
    if code in {"runtime_mismatch", "invalid_engine_output"}:
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
