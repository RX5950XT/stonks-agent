"""Production app assembly for the fixed NautilusTrader sidecar runtime."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from sidecars.nautilus.adapter import (
    AdapterPolicy,
    NautilusAdapter,
    compute_runtime_hash,
)
from sidecars.nautilus.app import create_app
from sidecars.nautilus.engine import NautilusEngineBackend
from stonks_contracts.backtest import (
    BacktestEngineKind,
    BacktestRuntimeIdentity,
)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required runtime setting is missing: {name}")
    return value


_runtime_root = Path(__file__).resolve().parent
_configured_runtime_hash = _required("STONKS_NAUTILUS_RUNTIME_HASH")
if _configured_runtime_hash != compute_runtime_hash(_runtime_root):
    raise RuntimeError("configured Nautilus runtime hash does not match build inputs")

_backend = NautilusEngineBackend(
    max_schedule_children=int(
        os.environ.get("STONKS_NAUTILUS_MAX_SCHEDULE_CHILDREN", "100000")
    )
)
_runtime = BacktestRuntimeIdentity(
    engine=BacktestEngineKind.NAUTILUS,
    engine_version=_backend.engine_version,
    adapter_version="0.1.0",
    runtime_hash=_configured_runtime_hash,
    image_digest=_required("STONKS_NAUTILUS_IMAGE_DIGEST"),
    deterministic=True,
)
_adapter = NautilusAdapter(
    policy=AdapterPolicy(
        runtime=_runtime,
        max_orders=int(os.environ.get("STONKS_NAUTILUS_MAX_ORDERS", "10000")),
        max_bars=int(os.environ.get("STONKS_NAUTILUS_MAX_BARS", "1000000")),
        max_order_bar_evaluations=int(
            os.environ.get("STONKS_NAUTILUS_MAX_ORDER_BAR_EVALUATIONS", "5000000")
        ),
    ),
    backend=_backend,
    clock=lambda: datetime.now(UTC),
)

app = create_app(
    adapter=_adapter,
    service_token=_required("STONKS_NAUTILUS_SERVICE_TOKEN"),
    max_concurrency=int(os.environ.get("STONKS_NAUTILUS_MAX_CONCURRENCY", "1")),
    max_request_bytes=int(
        os.environ.get("STONKS_NAUTILUS_MAX_REQUEST_BYTES", "16777216")
    ),
)
