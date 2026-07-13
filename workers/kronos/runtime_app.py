"""Production application factory that warms the pinned model exactly once."""

from __future__ import annotations

import os
from pathlib import Path

from workers.kronos.adapter import (
    KronosWorker,
    KronosWorkerPolicy,
    validate_worker_environment,
)
from workers.kronos.app import create_app
from workers.kronos.model_loader import (
    WarmOnceModelLoader,
    create_native_runtime,
    load_model_manifest,
)

_WORKER_ROOT = Path(__file__).resolve().parent
_environment = validate_worker_environment(os.environ)
_manifest = load_model_manifest(_WORKER_ROOT / "model-manifest.json")
_loader = WarmOnceModelLoader(
    root=_environment.model_root,
    manifest=_manifest,
    profile=_environment.profile.value,
    factory=create_native_runtime,
)
_loader.warm()
_worker = KronosWorker(
    policy=KronosWorkerPolicy(
        worker_version="kronos-worker/0.1.0",
        profile=_environment.profile,
        upstream_commit=_manifest.upstream_commit,
        model_revision=_manifest.model.revision,
        tokenizer_revision=_manifest.tokenizer.revision,
        manifest_hash=_manifest.payload_hash(),
    ),
    loader=_loader,
)

app = create_app(worker=_worker)
