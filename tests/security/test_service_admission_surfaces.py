from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sidecars.lean.app import create_app as create_lean_app  # noqa: E402
from sidecars.nautilus.app import create_app as create_nautilus_app  # noqa: E402
from sidecars.openbb.surface import build_surface  # noqa: E402
from workers.kronos.app import create_app as create_kronos_app  # noqa: E402
from workers.quant_lab.app import create_app as create_quant_lab_app  # noqa: E402
from workers.tradingagents.app import (  # noqa: E402
    create_app as create_tradingagents_app,
)

type AppFactory = Callable[[], FastAPI]


def _unreachable() -> Any:
    return cast(Any, object())


@pytest.mark.parametrize(
    ("factory", "path"),
    [
        (
            lambda: create_kronos_app(
                worker=_unreachable(),
                authenticator=_unreachable(),
            ),
            "/v1/preflight",
        ),
        (
            lambda: create_tradingagents_app(
                worker=_unreachable(),
                authenticator=_unreachable(),
            ),
            "/v1/analyze",
        ),
        (
            lambda: create_quant_lab_app(
                worker=_unreachable(),
                authenticator=_unreachable(),
            ),
            "/v1/research",
        ),
        (
            lambda: create_lean_app(
                adapter=_unreachable(),
                authenticator=_unreachable(),
                max_request_bytes=1_024,
            ),
            "/v1/backtests",
        ),
        (
            lambda: create_nautilus_app(
                adapter=_unreachable(),
                authenticator=_unreachable(),
                max_request_bytes=1_024,
            ),
            "/v1/backtests",
        ),
    ],
)
def test_fastapi_surfaces_reject_forwarded_identity_before_authentication(
    factory: AppFactory,
    path: str,
) -> None:
    response = TestClient(factory()).post(
        path,
        json={},
        headers={
            "authorization": "Bearer attacker-controlled",
            "x-forwarded-for": "203.0.113.9",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "invalid_request",
        "message": "Forwarded client identity is not accepted",
    }


def test_openbb_surface_rejects_forwarded_identity_with_safe_contract() -> None:
    async def downstream(
        _scope: dict[str, Any],
        _receive: Callable[[], Awaitable[dict[str, Any]]],
        _send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        raise AssertionError("forwarded request must not reach OpenBB")

    response = TestClient(build_surface(downstream, authenticator=_unreachable())).get(
        "/api/v1/equity/price/historical",
        params={"symbol": "AAPL", "provider": "yfinance"},
        headers={
            "authorization": "Bearer attacker-controlled",
            "forwarded": "for=203.0.113.9",
        },
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Forwarded client identity is not accepted"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["link"].startswith("</source>")


@pytest.mark.parametrize(
    "dockerfile",
    [
        ROOT / "workers" / "kronos" / "Dockerfile",
        ROOT / "workers" / "tradingagents" / "Dockerfile",
        ROOT / "workers" / "quant_lab" / "Dockerfile",
        ROOT / "sidecars" / "lean" / "Dockerfile",
        ROOT / "sidecars" / "nautilus" / "Dockerfile",
        ROOT / "sidecars" / "openbb" / "Dockerfile",
    ],
)
def test_service_runtime_never_rewrites_direct_peer_from_proxy_headers(
    dockerfile: Path,
) -> None:
    commands = [
        line
        for line in dockerfile.read_text(encoding="utf-8").splitlines()
        if line.startswith("CMD ") and "uvicorn" in line
    ]

    assert commands
    assert all('"--no-proxy-headers"' in command for command in commands)
