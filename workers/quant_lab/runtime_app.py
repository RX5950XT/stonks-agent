"""Production application for the fixed quant-lab runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from stonks_contracts.quant_lab import QuantRuntimeIdentity
from workers.quant_lab.app import create_app
from workers.quant_lab.qlib_adapter import (
    QlibLinearRuntime,
    QuantLabWorker,
    WorkerPolicy,
    compute_runtime_hash,
)


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_rows: int = Field(ge=2, le=1_000_000)
    max_request_bytes: int = Field(ge=1, le=16_777_216)
    runtime: QuantRuntimeIdentity


def load_settings(path: Path) -> RuntimeSettings:
    payload: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    settings = RuntimeSettings.model_validate(payload)
    calculated = compute_runtime_hash(Path(__file__).resolve().parent)
    if settings.runtime.runtime_hash != calculated:
        raise RuntimeError("quant-lab runtime hash does not match configuration")
    return settings


_ROOT = Path(__file__).resolve().parents[2]
_settings = load_settings(_ROOT / "config" / "workers" / "quant_lab.yaml")
_worker = QuantLabWorker(
    policy=WorkerPolicy(runtime=_settings.runtime, max_rows=_settings.max_rows),
    runtime=QlibLinearRuntime(_settings.runtime),
)

app = create_app(
    worker=_worker,
    max_request_bytes=_settings.max_request_bytes,
)
