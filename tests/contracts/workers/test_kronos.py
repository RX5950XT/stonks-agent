from __future__ import annotations

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

ROOT = Path(__file__).parents[3]
WORKER = ROOT / "workers" / "kronos"
sys.path.insert(0, str(ROOT))

from workers.kronos.adapter import (  # noqa: E402
    KronosPreflightRequest,
    KronosWorker,
    KronosWorkerPolicy,
    WorkerDeviceProfile,
    validate_worker_environment,
)
from workers.kronos.app import create_app  # noqa: E402
from workers.kronos.model_loader import (  # noqa: E402
    KronosModelManifest,
    ModelComponent,
    ModelFile,
    ModelLoadError,
    ValidatedModelPaths,
    WarmOnceModelLoader,
    load_model_manifest,
    validate_model_root,
)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _component(
    *, component_id: str, revision: str, directory: str, files: dict[str, bytes]
) -> ModelComponent:
    return ModelComponent(
        component_id=component_id,
        repository=f"NeoQuasar/{component_id}",
        revision=revision,
        directory=directory,
        files=tuple(
            ModelFile(path=name, size_bytes=len(content), sha256=_sha(content))
            for name, content in files.items()
        ),
    )


def _manifest(
    model_files: dict[str, bytes], tokenizer_files: dict[str, bytes]
) -> KronosModelManifest:
    return KronosModelManifest(
        manifest_version="1.0.0",
        upstream_repository="https://github.com/shiyu-coder/Kronos",
        upstream_commit="67b630e67f6a18c9e9be918d9b4337c960db1e9a",
        source_archive_sha256="969719e47b2134d8a56533784508b6d859bd1c9aacb1b62e4a504cb4fc096021",
        max_context=512,
        model=_component(
            component_id="Kronos-small",
            revision="901c26c1332695a2a8f243eb2f37243a37bea320",
            directory="kronos-small",
            files=model_files,
        ),
        tokenizer=_component(
            component_id="Kronos-Tokenizer-base",
            revision="0e0117387f39004a9016484a186a908917e22426",
            directory="kronos-tokenizer-base",
            files=tokenizer_files,
        ),
    )


def _model_root(tmp_path: Path) -> tuple[Path, KronosModelManifest]:
    model_files = {"config.json": b'{"model":1}', "model.safetensors": b"model"}
    tokenizer_files = {
        "config.json": b'{"tokenizer":1}',
        "model.safetensors": b"tokenizer",
    }
    manifest = _manifest(model_files, tokenizer_files)
    root = tmp_path / "models"
    for directory, files in (
        (manifest.model.directory, model_files),
        (manifest.tokenizer.directory, tokenizer_files),
    ):
        target = root / directory
        target.mkdir(parents=True)
        for name, content in files.items():
            (target / name).write_bytes(content)
    return root, manifest


def _policy(manifest: KronosModelManifest) -> KronosWorkerPolicy:
    return KronosWorkerPolicy(
        worker_version="kronos-worker/0.1.0",
        profile=WorkerDeviceProfile.CPU,
        upstream_commit=manifest.upstream_commit,
        model_revision=manifest.model.revision,
        tokenizer_revision=manifest.tokenizer.revision,
        manifest_hash=manifest.payload_hash(),
    )


def _request(
    manifest: KronosModelManifest, **overrides: object
) -> KronosPreflightRequest:
    values: dict[str, object] = {
        "request_id": "966e9ceb-e8cf-4402-b60a-cddf055d1cec",
        "profile": "cpu",
        "upstream_commit": manifest.upstream_commit,
        "model_revision": manifest.model.revision,
        "tokenizer_revision": manifest.tokenizer.revision,
        "manifest_hash": manifest.payload_hash(),
    }
    values.update(overrides)
    return KronosPreflightRequest.model_validate(values)


def test_pinned_manifest_has_exact_source_model_and_tokenizer_identities() -> None:
    manifest = load_model_manifest(WORKER / "model-manifest.json")

    assert manifest.upstream_commit == "67b630e67f6a18c9e9be918d9b4337c960db1e9a"
    assert (
        manifest.source_archive_sha256
        == "969719e47b2134d8a56533784508b6d859bd1c9aacb1b62e4a504cb4fc096021"
    )
    assert manifest.model.revision == "901c26c1332695a2a8f243eb2f37243a37bea320"
    assert manifest.tokenizer.revision == "0e0117387f39004a9016484a186a908917e22426"
    assert {item.path for item in manifest.model.files} == {
        "config.json",
        "model.safetensors",
    }
    assert {item.path for item in manifest.tokenizer.files} == {
        "config.json",
        "model.safetensors",
    }
    assert all(
        len(item.sha256) == 64
        for component in (manifest.model, manifest.tokenizer)
        for item in component.files
    )


def test_model_root_is_content_verified_before_runtime_factory(tmp_path: Path) -> None:
    root, manifest = _model_root(tmp_path)

    paths = validate_model_root(root, manifest)

    assert paths.root == root.resolve()
    assert paths.model_dir == (root / "kronos-small").resolve()
    assert paths.tokenizer_dir == (root / "kronos-tokenizer-base").resolve()


@pytest.mark.parametrize("fault", ["missing", "size", "hash", "unexpected"])
def test_model_root_drift_fails_closed(tmp_path: Path, fault: str) -> None:
    root, manifest = _model_root(tmp_path)
    target = root / manifest.model.directory / "model.safetensors"
    if fault == "missing":
        target.unlink()
    elif fault == "size":
        target.write_bytes(b"model-extra")
    elif fault == "hash":
        target.write_bytes(b"other")
    else:
        (target.parent / "untracked.bin").write_bytes(b"surprise")

    with pytest.raises(ModelLoadError):
        validate_model_root(root, manifest)


def test_symlinked_model_file_fails_closed(tmp_path: Path) -> None:
    root, manifest = _model_root(tmp_path)
    target = root / manifest.model.directory / "model.safetensors"
    external = tmp_path / "external.bin"
    external.write_bytes(target.read_bytes())
    target.unlink()
    try:
        target.symlink_to(external)
    except OSError:
        pytest.skip("symlinks are not available")

    with pytest.raises(ModelLoadError, match="symlink"):
        validate_model_root(root, manifest)


def test_manifest_rejects_remote_or_traversing_runtime_paths() -> None:
    values = _manifest(
        {"config.json": b"{}", "model.safetensors": b"x"},
        {"config.json": b"{}", "model.safetensors": b"y"},
    ).model_dump(mode="json")
    values["model"]["directory"] = "../outside"

    with pytest.raises(ValidationError):
        KronosModelManifest.model_validate(values)


def test_warm_once_loader_is_thread_safe_and_never_lazy_loads(tmp_path: Path) -> None:
    root, manifest = _model_root(tmp_path)
    calls: list[ValidatedModelPaths] = []
    runtime = object()

    def factory(paths: ValidatedModelPaths, profile: str) -> object:
        calls.append(paths)
        assert profile == "cpu"
        return runtime

    loader = WarmOnceModelLoader(
        root=root, manifest=manifest, profile="cpu", factory=factory
    )
    with pytest.raises(ModelLoadError, match="not warmed"):
        loader.get()

    with ThreadPoolExecutor(max_workers=8) as pool:
        loaded = tuple(pool.map(lambda _: loader.warm(), range(24)))

    assert loaded == (runtime,) * 24
    assert loader.get() is runtime
    assert len(calls) == 1


def test_warm_failure_is_memoized_and_never_retried(tmp_path: Path) -> None:
    root, manifest = _model_root(tmp_path)
    calls = 0

    def fail(_paths: ValidatedModelPaths, _profile: str) -> object:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    loader = WarmOnceModelLoader(
        root=root, manifest=manifest, profile="cpu", factory=fail
    )
    for _ in range(2):
        with pytest.raises(ModelLoadError, match="startup model load failed"):
            loader.warm()
    assert calls == 1


def test_preflight_binds_exact_warmed_runtime_without_order_authority(
    tmp_path: Path,
) -> None:
    root, manifest = _model_root(tmp_path)
    loader = WarmOnceModelLoader(
        root=root,
        manifest=manifest,
        profile="cpu",
        factory=lambda _paths, _profile: object(),
    )
    worker = KronosWorker(policy=_policy(manifest), loader=loader)

    unavailable = worker.preflight(_request(manifest))
    loader.warm()
    accepted = worker.preflight(_request(manifest))
    mismatch = worker.preflight(_request(manifest, model_revision="a" * 40))

    assert unavailable.error is not None and unavailable.error.code == "model_not_ready"
    assert accepted.value is not None and accepted.value.ready is True
    assert mismatch.error is not None and mismatch.error.code == "runtime_mismatch"
    with pytest.raises(ValidationError):
        KronosPreflightRequest.model_validate(
            {**_request(manifest).model_dump(mode="json"), "order_intent": {}}
        )


def test_http_liveness_readiness_and_bounded_preflight(tmp_path: Path) -> None:
    root, manifest = _model_root(tmp_path)
    loader = WarmOnceModelLoader(
        root=root,
        manifest=manifest,
        profile="cpu",
        factory=lambda _paths, _profile: object(),
    )
    worker = KronosWorker(policy=_policy(manifest), loader=loader)
    client = TestClient(create_app(worker=worker, max_request_bytes=4_096))

    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 503
    loader.warm()
    ready = client.get("/readyz")
    accepted = client.post(
        "/v1/preflight", json=_request(manifest).model_dump(mode="json")
    )
    invalid = client.post(
        "/v1/preflight", content=b"{}", headers={"content-type": "text/plain"}
    )
    oversized = client.post(
        "/v1/preflight",
        content=b"x" * 4_097,
        headers={"content-type": "application/json"},
    )

    assert ready.status_code == 200 and ready.json()["data"]["ready"] is True
    assert accepted.status_code == 200 and accepted.json()["success"] is True
    assert invalid.status_code == 415
    assert oversized.status_code == 413


@pytest.mark.parametrize(
    "forbidden",
    [
        "DATABASE_URL",
        "POSTGRES_PASSWORD",
        "BROKER_TOKEN",
        "REDIS_URL",
        "QUEUE_URL",
        "OPENAI_API_KEY",
        "HF_TOKEN",
        "HF_HOME",
        "HUGGINGFACE_HUB_CACHE",
        "HTTP_PROXY",
    ],
)
def test_worker_environment_denies_credentials_network_and_cache_fallback(
    forbidden: str,
) -> None:
    with pytest.raises(ValueError, match="forbidden worker environment"):
        validate_worker_environment(
            {
                "STONKS_KRONOS_PROFILE": "cpu",
                "STONKS_KRONOS_MODEL_ROOT": "/models",
                forbidden: "must-not-enter-worker",
            }
        )


def test_worker_environment_accepts_only_local_absolute_model_root() -> None:
    environment = validate_worker_environment(
        {
            "STONKS_KRONOS_PROFILE": "cuda",
            "STONKS_KRONOS_MODEL_ROOT": "/models",
        }
    )
    assert environment.profile is WorkerDeviceProfile.CUDA
    assert environment.model_root == Path("/models")

    with pytest.raises(ValueError):
        validate_worker_environment(
            {
                "STONKS_KRONOS_PROFILE": "cpu",
                "STONKS_KRONOS_MODEL_ROOT": "https://host/model",
            }
        )


def test_worker_files_keep_profiles_isolated_and_runtime_hardened() -> None:
    cpu_project = (WORKER / "pyproject.toml").read_text(encoding="utf-8")
    cpu_lock = (WORKER / "uv.lock").read_text(encoding="utf-8")
    cuda_project = (WORKER / "profiles" / "cuda" / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    cuda_lock = (WORKER / "profiles" / "cuda" / "uv.lock").read_text(encoding="utf-8")
    dockerfile = (WORKER / "Dockerfile").read_text(encoding="utf-8")
    compose = yaml.safe_load(
        (ROOT / "infra" / "compose.kronos.yaml").read_text(encoding="utf-8")
    )

    assert "download.pytorch.org/whl/cpu" in cpu_project
    assert "download.pytorch.org/whl/cu" in cuda_project
    assert 'name = "torch"' in cpu_lock and 'name = "torch"' in cuda_lock
    assert (
        "ADD --checksum=sha256:969719e47b2134d8a56533784508b6d859bd1c9aacb1b62e4a504cb4fc096021"
        in dockerfile
    )
    assert "USER 65532:65532" in dockerfile
    assert "FROM cpu AS runtime-cpu" in dockerfile
    assert "FROM cuda AS runtime-cuda" in dockerfile
    for name in ("kronos-cpu", "kronos-cuda"):
        service = compose["services"][name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["networks"] == ["kronos-internal"]
        assert service["volumes"][0].endswith(":/models:ro")
    assert compose["networks"]["kronos-internal"]["internal"] is True


def test_kronos_license_notice_and_core_lock_isolation() -> None:
    manifest = yaml.safe_load(
        (ROOT / "docs" / "legal" / "upstream-manifest.yaml").read_text(encoding="utf-8")
    )
    kronos = next(item for item in manifest["upstreams"] if item["id"] == "kronos")
    notice = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    worker_notice = (WORKER / "NOTICE.md").read_text(encoding="utf-8")
    core_lock = (ROOT / "uv.lock").read_text(encoding="utf-8").lower()

    assert kronos["notice"] == {"required": True, "id": "KRONOS-MIT-WORKER"}
    assert "KRONOS-MIT-WORKER" in notice
    assert "Copyright (c) 2025 ShiYu" in worker_notice
    assert 'name = "torch"' not in core_lock


def test_manifest_json_is_canonical_and_has_no_download_url() -> None:
    raw = (WORKER / "model-manifest.json").read_bytes()
    payload: dict[str, Any] = json.loads(raw)
    assert raw.endswith(b"\n")
    assert (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode()
        + b"\n"
        == raw
    )
    assert "url" not in json.dumps(payload).lower()
