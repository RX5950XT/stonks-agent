# SPDX-License-Identifier: AGPL-3.0-only
"""AGPL-3.0-only OpenBB app wrapper with a network source offer."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from openbb_core.api.rest_api import app as openbb_app
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import FileResponse, JSONResponse, Response
from surface import SurfaceAllowlist

SOURCE_ARCHIVE = Path("/srv/stonks-openbb-sidecar-source.tar.gz")
SOURCE_LINK = '</source>; rel="source"; type="application/gzip"'


@openbb_app.middleware("http")
async def advertise_source(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    """Prominently advertise the corresponding-source endpoint."""

    response = await call_next(request)
    response.headers["Link"] = SOURCE_LINK
    response.headers["X-Corresponding-Source"] = "/source"
    return response


@openbb_app.get(
    "/source",
    include_in_schema=True,
    summary="Download Corresponding Source",
    description="AGPL-3.0 section 13 source archive and exact build recipe.",
    tags=["legal"],
)
async def corresponding_source() -> FileResponse:
    """Serve this deployed sidecar's build inputs without charge."""

    return FileResponse(
        SOURCE_ARCHIVE,
        media_type="application/gzip",
        filename=SOURCE_ARCHIVE.name,
        headers={"Link": SOURCE_LINK},
    )


@openbb_app.get(
    "/healthz",
    include_in_schema=True,
    summary="Sidecar liveness",
    tags=["health"],
)
async def healthz() -> JSONResponse:
    """Report immutable build identity without making a provider request."""

    return JSONResponse(
        {
            "status": "ok",
            "provider": "yfinance",
            "source": "/source",
        }
    )


app = SurfaceAllowlist(openbb_app)
