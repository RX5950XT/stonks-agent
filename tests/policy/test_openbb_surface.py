from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SURFACE = ROOT / "sidecars" / "openbb" / "surface.py"
HISTORICAL = "/api/v1/equity/price/historical"


def _load_surface() -> Any:
    spec = spec_from_file_location("openbb_surface_under_test", SURFACE)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exercise(
    middleware: Any,
    *,
    scope_type: str = "http",
    method: str = "GET",
    path: str = HISTORICAL,
) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": f"{scope_type}.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": scope_type, "method": method, "path": path}
    asyncio.run(middleware(scope, receive, send))
    return sent


@pytest.mark.parametrize("path", [HISTORICAL, "/healthz", "/source"])
def test_exact_get_surface_reaches_downstream(path: str) -> None:
    surface = _load_surface()
    calls: list[dict[str, Any]] = []

    async def downstream(
        scope: dict[str, Any],
        _receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        calls.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = _exercise(surface.SurfaceAllowlist(downstream), path=path)

    assert len(calls) == 1
    assert sent[0]["status"] == 204


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", HISTORICAL),
        ("OPTIONS", HISTORICAL),
        ("HEAD", "/healthz"),
        ("GET", f"{HISTORICAL}/"),
        ("GET", "/api/v1/equity/price/quote"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("GET", "/"),
    ],
)
def test_every_other_http_request_is_hidden_before_downstream(
    method: str,
    path: str,
) -> None:
    surface = _load_surface()
    calls = 0

    async def downstream(*_args: object) -> None:
        nonlocal calls
        calls += 1

    sent = _exercise(
        surface.SurfaceAllowlist(downstream),
        method=method,
        path=path,
    )

    assert calls == 0
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 404
    assert (b"link", surface.SOURCE_LINK.encode("ascii")) in sent[0]["headers"]
    assert sent[1] == {
        "type": "http.response.body",
        "body": b'{"detail":"Not Found"}',
    }


def test_websocket_is_closed_without_reaching_downstream() -> None:
    surface = _load_surface()
    calls = 0

    async def downstream(*_args: object) -> None:
        nonlocal calls
        calls += 1

    sent = _exercise(
        surface.SurfaceAllowlist(downstream),
        scope_type="websocket",
        path=HISTORICAL,
    )

    assert calls == 0
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_lifespan_only_is_forwarded_to_downstream() -> None:
    surface = _load_surface()
    calls = 0

    async def downstream(*_args: object) -> None:
        nonlocal calls
        calls += 1

    sent = _exercise(
        surface.SurfaceAllowlist(downstream),
        scope_type="lifespan",
        path="",
    )

    assert calls == 1
    assert sent == []
