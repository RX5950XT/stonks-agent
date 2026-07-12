from __future__ import annotations

import asyncio
import json
from collections import deque

from starlette.types import Message, Receive, Scope, Send

from stonks_agent.entrypoints.api.request_limits import RequestBodyLimitMiddleware


def test_streamed_body_without_content_length_is_bounded() -> None:
    incoming: deque[Message] = deque(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5678", "more_body": False},
        ]
    )
    outgoing: list[Message] = []

    async def receive() -> Message:
        return incoming.popleft()

    async def send(message: Message) -> None:
        outgoing.append(message)

    async def consume_body(scope: Scope, receive: Receive, send: Send) -> None:
        del scope
        while (await receive()).get("more_body", False):
            pass
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/upload",
        "raw_path": b"/upload",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "state": {},
    }

    asyncio.run(
        RequestBodyLimitMiddleware(consume_body, max_bytes=5)(scope, receive, send)
    )

    assert outgoing[0]["status"] == 413
    payload = json.loads(outgoing[1]["body"])
    assert payload["error"]["code"] == "payload_too_large"


def test_body_is_bounded_before_downstream_can_start_success_response() -> None:
    incoming: deque[Message] = deque(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5678", "more_body": False},
        ]
    )
    outgoing: list[Message] = []

    async def receive() -> Message:
        return incoming.popleft()

    async def send(message: Message) -> None:
        outgoing.append(message)

    async def premature_success(scope: Scope, receive: Receive, send: Send) -> None:
        del scope
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await receive()
        await receive()
        await send({"type": "http.response.body", "body": b""})

    asyncio.run(
        RequestBodyLimitMiddleware(premature_success, max_bytes=5)(
            _http_scope(),
            receive,
            send,
        )
    )

    starts = [item for item in outgoing if item["type"] == "http.response.start"]
    assert [item["status"] for item in starts] == [413]


def _http_scope() -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/upload",
        "raw_path": b"/upload",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "state": {},
    }


def test_excessively_long_digit_content_length_fails_closed_without_parsing() -> None:
    called = False
    outgoing: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        outgoing.append(message)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        del scope, receive, send
        called = True

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/upload",
        "raw_path": b"/upload",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-length", b"9" * 5000)],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "state": {},
    }

    asyncio.run(RequestBodyLimitMiddleware(app, max_bytes=5)(scope, receive, send))

    assert called is False
    assert outgoing[0]["status"] == 413
    payload = json.loads(outgoing[1]["body"])
    assert payload["error"]["code"] == "payload_too_large"
