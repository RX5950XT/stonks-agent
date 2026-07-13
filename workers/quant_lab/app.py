"""Bounded HTTP surface for the isolated quant-lab worker."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from stonks_contracts.quant_lab import QuantResearchJob
from workers.quant_lab.qlib_adapter import QuantLabWorker, WorkerFailure


def create_app(
    *, worker: QuantLabWorker, max_request_bytes: int = 16_777_216
) -> FastAPI:
    if not 1 <= max_request_bytes <= 16_777_216:
        raise ValueError("max_request_bytes is outside the supported range")
    app = FastAPI(
        title="Stonks Quant Lab Worker",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

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
        rejection = _validate_headers(incoming, max_request_bytes)
        if rejection is not None:
            return rejection
        body = await _read_bounded(incoming, max_request_bytes)
        if body is None:
            return _error(413, "request_too_large", "Worker request is too large")
        try:
            job = QuantResearchJob.model_validate_json(body)
        except (ValidationError, json.JSONDecodeError):
            return _error(400, "invalid_request", "Worker request is invalid")
        outcome = worker.research(job)
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
    if declared is not None and (not declared.isdecimal() or int(declared) > maximum):
        return _error(413, "request_too_large", "Worker request is too large")
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
    if code == "deadline_expired":
        return 408
    if code == "dataset_too_large":
        return 413
    if code in {"runtime_mismatch", "split_invalid"}:
        return 409
    return 503


def _error(status: int, code: str, message: str) -> JSONResponse:
    return _envelope(status, error={"code": code, "message": message})


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
