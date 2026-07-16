"""Production app assembly for the fixed QuantConnect LEAN sidecar runtime."""

from __future__ import annotations

import importlib
import os
from datetime import UTC, datetime
from pathlib import Path

from sidecars.lean.adapter import (
    AdapterPolicy,
    LeanAdapter,
    compute_runtime_hash,
)
from sidecars.lean.app import create_app
from stonks_contracts.backtest import (
    BacktestEngineKind,
    BacktestRuntimeIdentity,
)
from stonks_service_auth import (
    load_static_oidc_service_authenticator,
    validate_isolated_runtime_environment,
)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required runtime setting is missing: {name}")
    return value


_runtime_root = Path(__file__).resolve().parent
validate_isolated_runtime_environment(os.environ)
_authenticator = load_static_oidc_service_authenticator(os.environ)
_configured_runtime_hash = _required("STONKS_LEAN_RUNTIME_HASH")
if _configured_runtime_hash != compute_runtime_hash(_runtime_root):
    raise RuntimeError("configured LEAN runtime hash does not match build inputs")

_backend_class = importlib.import_module("sidecars.lean.engine").LeanEngineBackend
_backend = _backend_class(
    max_schedule_children=int(
        os.environ.get("STONKS_LEAN_MAX_SCHEDULE_CHILDREN", "100000")
    ),
    max_engine_seconds=int(os.environ.get("STONKS_LEAN_MAX_ENGINE_SECONDS", "120")),
    max_trace_bytes=int(os.environ.get("STONKS_LEAN_MAX_TRACE_BYTES", "16777216")),
)
_runtime = BacktestRuntimeIdentity(
    engine=BacktestEngineKind.LEAN,
    engine_version=_backend.engine_version,
    adapter_version="0.1.0",
    runtime_hash=_configured_runtime_hash,
    image_digest=_required("STONKS_LEAN_IMAGE_DIGEST"),
    deterministic=True,
)
_adapter = LeanAdapter(
    policy=AdapterPolicy(
        runtime=_runtime,
        max_orders=int(os.environ.get("STONKS_LEAN_MAX_ORDERS", "10000")),
        max_bars=int(os.environ.get("STONKS_LEAN_MAX_BARS", "1000000")),
        max_order_bar_evaluations=int(
            os.environ.get("STONKS_LEAN_MAX_ORDER_BAR_EVALUATIONS", "5000000")
        ),
    ),
    backend=_backend,
    clock=lambda: datetime.now(UTC),
)

app = create_app(
    adapter=_adapter,
    authenticator=_authenticator,
    max_concurrency=int(os.environ.get("STONKS_LEAN_MAX_CONCURRENCY", "1")),
    max_request_bytes=int(os.environ.get("STONKS_LEAN_MAX_REQUEST_BYTES", "16777216")),
)
