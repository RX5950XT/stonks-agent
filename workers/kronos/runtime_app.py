"""Production application factory that warms the pinned model exactly once."""

from __future__ import annotations

import os
from collections.abc import Mapping
from importlib.metadata import version
from pathlib import Path

from stonks_service_auth import (
    load_static_oidc_service_authenticator,
    validate_isolated_runtime_environment,
)
from workers.kronos.adapter import (
    KronosWorker,
    KronosWorkerPolicy,
    validate_worker_environment,
)
from workers.kronos.app import create_app
from workers.kronos.model_loader import (
    WarmOnceModelLoader,
    compute_runtime_hash,
    create_native_runtime,
    load_model_manifest,
)

_WORKER_ROOT = Path(__file__).resolve().parent


def _execution_concurrency(environment: Mapping[str, str]) -> int:
    if environment.get("STONKS_KRONOS_MAX_CONCURRENCY", "1") != "1":
        raise RuntimeError("Kronos execution concurrency must be exactly one")
    return 1


validate_isolated_runtime_environment(os.environ)
_authenticator = load_static_oidc_service_authenticator(os.environ)
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
        worker_version="kronos-worker/0.2.0",
        profile=_environment.profile,
        upstream_commit=_manifest.upstream_commit,
        model_id=_manifest.model.repository,
        model_revision=_manifest.model.revision,
        model_artifact_hash=next(
            item.sha256
            for item in _manifest.model.files
            if item.path == "model.safetensors"
        ),
        tokenizer_id=_manifest.tokenizer.repository,
        tokenizer_revision=_manifest.tokenizer.revision,
        tokenizer_artifact_hash=next(
            item.sha256
            for item in _manifest.tokenizer.files
            if item.path == "model.safetensors"
        ),
        manifest_hash=_manifest.payload_hash(),
        runtime_hash=compute_runtime_hash(_WORKER_ROOT, _environment.profile.value),
        torch_version=version("torch"),
        inference_code_version="kronos-path-retention/1.0.0",
    ),
    loader=_loader,
)

app = create_app(
    worker=_worker,
    authenticator=_authenticator,
    max_concurrency=_execution_concurrency(os.environ),
)
