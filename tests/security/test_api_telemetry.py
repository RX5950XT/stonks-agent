from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, cast

import pytest
from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from stonks_agent.adapters.observability.context import current_trace_context
from stonks_agent.adapters.observability.operation import OperationRecorder
from stonks_agent.domain.errors import Result, Success
from stonks_agent.domain.telemetry import (
    ComponentName,
    OperationName,
    TraceContext,
)
from stonks_agent.entrypoints.api.api_security import (
    ApiSecurityOptions,
    ApiSecurityPolicy,
    install_api_security,
)
from stonks_agent.entrypoints.api.envelope import success_envelope
from stonks_agent.entrypoints.api.telemetry import (
    ApiTelemetryOptions,
    install_api_telemetry,
)
from stonks_agent.entrypoints.api.web_protection import SECURITY_RESPONSE_HEADERS

INCOMING_TRACE = "00-11111111111111111111111111111111-2222222222222222-01"
CONTINUED_TRACE = "00-11111111111111111111111111111111-bbbbbbbbbbbbbbbb-01"
NEW_TRACE = "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01"


class FixedGenerator:
    def new_trace_id(self) -> str:
        return "a" * 32

    def new_span_id(self) -> str:
        return "b" * 16


class ExplodingGenerator:
    def new_trace_id(self) -> str:
        raise RuntimeError("secret generator failure")

    def new_span_id(self) -> str:
        raise RuntimeError("secret generator failure")


class RecordingRecorder:
    def __init__(self, *, explode: bool = False) -> None:
        self.calls: list[tuple[ComponentName, OperationName, TraceContext | None]] = []
        self.results: list[Result[object]] = []
        self.explode = explode

    def record_result[T](
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        call: Callable[[], Result[T]],
        parent: TraceContext | None = None,
    ) -> Result[T]:
        if self.explode:
            raise RuntimeError("telemetry backend unavailable")
        result = call()
        self.calls.append((component, operation, parent))
        self.results.append(result)
        return result

    async def record_async_result[T](
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        call: Callable[[], Awaitable[Result[T]]],
        parent: TraceContext | None = None,
    ) -> Result[T]:
        result = await call()
        self.calls.append((component, operation, parent))
        self.results.append(result)
        if self.explode:
            raise RuntimeError("telemetry backend unavailable")
        return result


class DuplicatingAsyncRecorder(RecordingRecorder):
    async def record_async_result[T](
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        call: Callable[[], Awaitable[Result[T]]],
        parent: TraceContext | None = None,
    ) -> Result[T]:
        first = await call()
        second = await call()
        assert second is first
        self.calls.append((component, operation, parent))
        return first


class ForgingAsyncRecorder(RecordingRecorder):
    async def record_async_result[T](
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        call: Callable[[], Awaitable[Result[T]]],
        parent: TraceContext | None = None,
    ) -> Result[T]:
        del component, operation, call, parent
        return cast(Result[T], Success(None))


class MutableClock:
    def __init__(self) -> None:
        self.value = 10.0
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.value


class Metrics:
    def __init__(self) -> None:
        self.duration: float | None = None

    def increment(
        self,
        name: str,
        value: int = 1,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        del name, value, attributes

    def observe(
        self,
        name: str,
        value: float,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        del name, attributes
        self.duration = value


class Span:
    def set_attribute(self, name: str, value: object) -> None:
        del name, value

    def record_error(self, error: object) -> None:
        del error

    def end(self) -> None:
        return


class Tracer:
    def start_span(
        self,
        name: str,
        *,
        parent: object = None,
        attributes: Mapping[str, str] | None = None,
    ) -> Span:
        del name, parent, attributes
        return Span()


def test_valid_w3c_parent_and_request_id_are_continued_and_returned() -> None:
    recorder = RecordingRecorder()
    app = _app(recorder=recorder)

    response = TestClient(app).get(
        "/context",
        headers={
            "traceparent": INCOMING_TRACE,
            "tracestate": "vendor=value",
            "x-request-id": "request-existing",
        },
    )

    assert response.status_code == 200
    assert response.headers["traceparent"] == CONTINUED_TRACE
    assert response.headers["tracestate"] == "vendor=value"
    assert response.headers["x-request-id"] == "request-existing"
    body = response.json()
    assert body["data"] == {
        "trace_id": "1" * 32,
        "span_id": "b" * 16,
        "request_id": "request-existing",
    }
    assert body["metadata"] == {
        "pagination": None,
        "request_id": "request-existing",
        "trace_id": "1" * 32,
    }
    assert recorder.calls == [
        (
            ComponentName.API,
            OperationName.HTTP_REQUEST,
            TraceContext(
                trace_id="1" * 32,
                span_id="b" * 16,
                trace_flags="01",
                tracestate="vendor=value",
                request_id="request-existing",
            ),
        )
    ]


def test_missing_context_gets_bounded_generated_context_and_overwrites_spoofed_response() -> (
    None
):
    app = _app(spoof_response_headers=True)

    response = TestClient(app).get("/context")

    assert response.status_code == 200
    assert response.headers["traceparent"] == NEW_TRACE
    assert "tracestate" not in response.headers
    assert response.headers["x-request-id"] == "request-generated"
    assert response.json()["metadata"]["request_id"] == "request-generated"
    assert response.json()["metadata"]["trace_id"] == "a" * 32


@pytest.mark.parametrize(
    "headers",
    (
        [("traceparent", INCOMING_TRACE), ("traceparent", INCOMING_TRACE)],
        [("tracestate", "vendor=value")],
        [
            (
                "traceparent",
                "00-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA-BBBBBBBBBBBBBBBB-01",
            )
        ],
        [("x-request-id", "contains space")],
        [("x-request-id", "one"), ("x-request-id", "two")],
    ),
)
def test_ambiguous_or_invalid_context_is_rejected_with_fresh_context(
    headers: list[tuple[str, str]],
) -> None:
    response = TestClient(_app()).get("/context", headers=headers)

    assert response.status_code == 400
    assert response.headers["traceparent"] == NEW_TRACE
    assert response.headers["x-request-id"] == "request-generated"
    assert response.json()["error"] == {
        "code": "invalid_input",
        "message": "Trace context is invalid",
        "details": {},
    }
    assert response.json()["metadata"]["trace_id"] == "a" * 32
    assert response.json()["metadata"]["request_id"] == "request-generated"
    for name, value in SECURITY_RESPONSE_HEADERS.items():
        assert response.headers[name] == value


@pytest.mark.parametrize(
    ("path", "method", "expected_status"),
    (
        ("/validated", "post", 400),
        ("/missing", "get", 404),
        ("/explode", "get", 500),
    ),
)
def test_framework_errors_always_include_trace_headers_and_metadata(
    path: str,
    method: str,
    expected_status: int,
) -> None:
    client = TestClient(_app(), raise_server_exceptions=False)

    response = client.request(method, path)

    assert response.status_code == expected_status
    assert response.headers["traceparent"] == NEW_TRACE
    assert response.headers["x-request-id"] == "request-generated"
    assert response.json()["metadata"] == {
        "pagination": None,
        "request_id": "request-generated",
        "trace_id": "a" * 32,
    }


def test_rate_limit_rejection_keeps_request_context() -> None:
    client = TestClient(_app(rate_limit=1))
    assert client.get("/context").status_code == 200

    response = client.get(
        "/context",
        headers={"x-request-id": "rate-limited-request"},
    )

    assert response.status_code == 429
    assert response.headers["traceparent"] == NEW_TRACE
    assert response.headers["x-request-id"] == "rate-limited-request"
    assert response.json()["metadata"]["request_id"] == "rate-limited-request"
    assert response.json()["metadata"]["trace_id"] == "a" * 32


def test_recorder_failure_never_changes_http_result() -> None:
    response = TestClient(_app(recorder=RecordingRecorder(explode=True))).get(
        "/context"
    )

    assert response.status_code == 200
    assert response.json()["success"] is True


def test_recorder_cannot_dispatch_one_http_request_twice() -> None:
    calls = 0
    app = FastAPI()
    install_api_security(app, max_request_bytes=1024)
    install_api_telemetry(
        app,
        options=ApiTelemetryOptions(
            generator=FixedGenerator(),
            request_id_factory=lambda: "request-generated",
            recorder=DuplicatingAsyncRecorder(),
        ),
    )

    def counted() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    app.add_api_route("/counted", counted, methods=["GET"])

    response = TestClient(app).get("/counted")

    assert response.status_code == 200
    assert response.json() == {"calls": 1}
    assert calls == 1


def test_recorder_cannot_skip_http_dispatch() -> None:
    calls = 0
    app = FastAPI()
    install_api_security(app, max_request_bytes=1024)
    install_api_telemetry(
        app,
        options=ApiTelemetryOptions(
            generator=FixedGenerator(),
            request_id_factory=lambda: "request-generated",
            recorder=ForgingAsyncRecorder(),
        ),
    )

    def counted() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    app.add_api_route("/counted", counted, methods=["GET"])

    response = TestClient(app).get("/counted")

    assert response.status_code == 200
    assert response.json() == {"calls": 1}
    assert calls == 1


@pytest.mark.parametrize("failure", ("request_id_factory", "generator"))
def test_telemetry_dependency_failure_falls_back_without_leaking(
    failure: str,
) -> None:
    options: dict[str, object] = {}
    if failure == "request_id_factory":
        options["request_id_factory"] = lambda: (_ for _ in ()).throw(
            RuntimeError("secret request ID failure")
        )
    else:
        options["generator"] = ExplodingGenerator()

    response = TestClient(_app(**options)).get("/context")

    assert response.status_code == 200
    request_id = response.headers["x-request-id"]
    traceparent = response.headers["traceparent"]
    if failure == "request_id_factory":
        assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    else:
        assert request_id == "request-generated"
    assert re.fullmatch(
        r"00-[0-9a-f]{32}-[0-9a-f]{16}-01",
        traceparent,
    )
    assert response.json()["metadata"]["request_id"] == request_id
    assert response.json()["metadata"]["trace_id"] == traceparent[3:35]
    assert "secret" not in response.text


def test_async_recorder_measures_the_entire_awaited_request() -> None:
    clock = MutableClock()
    metrics = Metrics()
    recorder = OperationRecorder(
        metrics=metrics,
        tracer=Tracer(),
        environment="test",
        clock=clock,
    )
    app = FastAPI()
    install_api_security(app, max_request_bytes=1024)
    install_api_telemetry(
        app,
        options=ApiTelemetryOptions(
            generator=FixedGenerator(),
            request_id_factory=lambda: "request-generated",
            recorder=recorder,
        ),
    )

    async def timed() -> dict[str, bool]:
        assert clock.calls == 1
        await asyncio.sleep(0)
        clock.value = 10.75
        return {"ok": True}

    app.add_api_route("/timed", timed, methods=["GET"])

    response = TestClient(app).get("/timed")

    assert response.status_code == 200
    assert metrics.duration == 0.75


def _app(
    *,
    recorder: RecordingRecorder | None = None,
    rate_limit: int = 120,
    spoof_response_headers: bool = False,
    generator: FixedGenerator | ExplodingGenerator | None = None,
    request_id_factory: Callable[[], str] | None = None,
) -> FastAPI:
    app = FastAPI()
    policy = ApiSecurityPolicy(
        rate_limit_requests=rate_limit,
        direct_peer_edge_requests=rate_limit,
    )
    install_api_security(
        app,
        max_request_bytes=1024,
        options=ApiSecurityOptions(policy=policy),
    )
    install_api_telemetry(
        app,
        options=ApiTelemetryOptions(
            generator=generator or FixedGenerator(),
            request_id_factory=(request_id_factory or (lambda: "request-generated")),
            recorder=recorder,
        ),
    )

    def context() -> JSONResponse:
        selected = current_trace_context()
        assert selected is not None
        headers = (
            {
                "traceparent": "spoofed",
                "tracestate": "spoofed",
                "x-request-id": "spoofed",
            }
            if spoof_response_headers
            else None
        )
        envelope = success_envelope(
            {
                "trace_id": selected.trace_id,
                "span_id": selected.span_id,
                "request_id": selected.request_id,
            }
        )
        return JSONResponse(
            content=envelope.model_dump(mode="json"),
            headers=headers,
        )

    def validated(payload: Annotated[dict[str, str], Body()]) -> dict[str, str]:
        return payload

    def explode() -> None:
        raise RuntimeError("must not leak")

    app.add_api_route("/context", context, methods=["GET"])
    app.add_api_route("/validated", validated, methods=["POST"])
    app.add_api_route("/explode", explode, methods=["GET"])
    return app
