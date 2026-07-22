from __future__ import annotations

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from stonks_contracts.kronos import (
    KronosForecastPoint,
    KronosWorkerRequest,
    VolumeQuality,
)
from stonks_service_auth import ServiceReceiver

ROOT = Path(__file__).parents[3]
WORKER = ROOT / "workers" / "kronos"
sys.path.insert(0, str(ROOT))

from fixtures.service_auth import (  # noqa: E402
    ExactServiceAuthenticator,
    authorization_headers,
)

from workers.kronos import model_loader as model_loader_module  # noqa: E402
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
    NativeKronosRuntime,
    ValidatedModelPaths,
    WarmOnceModelLoader,
    compute_runtime_hash,
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
        worker_version="kronos-worker/0.2.0",
        profile=WorkerDeviceProfile.CPU,
        upstream_commit=manifest.upstream_commit,
        model_id=manifest.model.repository,
        model_revision=manifest.model.revision,
        model_artifact_hash=manifest.model.files[1].sha256,
        tokenizer_id=manifest.tokenizer.repository,
        tokenizer_revision=manifest.tokenizer.revision,
        tokenizer_artifact_hash=manifest.tokenizer.files[1].sha256,
        manifest_hash=manifest.payload_hash(),
        runtime_hash="1" * 64,
        torch_version="2.12.1+cpu",
        inference_code_version="kronos-path-retention/1.0.0",
    )


def _forecast_request(
    policy: KronosWorkerPolicy, **overrides: object
) -> KronosWorkerRequest:
    as_of = datetime(2026, 1, 9, 21, tzinfo=UTC)
    values: dict[str, object] = {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "run_id": "22222222-2222-4222-8222-222222222222",
        "job_id": "33333333-3333-4333-8333-333333333333",
        "attempt_generation": 2,
        "attempt_nonce": "nonce-2",
        "profile": "cpu",
        "instrument_id": "44444444-4444-4444-8444-444444444444",
        "mic": "XNAS",
        "dataset_snapshot_id": "55555555-5555-4555-8555-555555555555",
        "snapshot_artifact_ref": f"sha256:{'a' * 64}",
        "data_hash": "b" * 64,
        "as_of": as_of,
        "interval": "1d",
        "bars": tuple(
            {
                "event_time": datetime(2026, 1, day, 21, tzinfo=UTC),
                "available_at": datetime(2026, 1, day, 21, tzinfo=UTC),
                "open": "100",
                "high": "102",
                "low": "99",
                "close": "101",
                "volume": "1000",
                "amount": "101000",
                "volume_quality": "observed",
            }
            for day in (7, 8, 9)
        ),
        "future_timestamps": (
            datetime(2026, 1, 12, 21, tzinfo=UTC),
            datetime(2026, 1, 13, 21, tzinfo=UTC),
        ),
        "runtime": policy.runtime_identity,
        "sampling": {
            "seed_policy": "explicit-sequential-v1",
            "seeds": (17, 18, 19),
            "temperature": "1",
            "top_k": 0,
            "top_p": "0.9",
        },
        "deadline": as_of + timedelta(minutes=5),
    }
    values.update(overrides)
    return KronosWorkerRequest.model_validate(values)


class FakePathRuntime:
    def __init__(self) -> None:
        self.seeds: list[int] = []
        self.timestamp_drift = False

    def predict_path(
        self, request: KronosWorkerRequest, *, seed: int
    ) -> tuple[KronosForecastPoint, ...]:
        self.seeds.append(seed)
        moments = request.future_timestamps
        if self.timestamp_drift:
            moments = (request.as_of, *moments[1:])
        return tuple(
            KronosForecastPoint(
                timestamp=moment,
                open=Decimal("101"),
                high=Decimal("103"),
                low=Decimal("100"),
                close=Decimal("102"),
                volume=Decimal("1001"),
                amount=Decimal("102102"),
            )
            for moment in moments
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


def test_forecast_runs_one_retained_path_per_explicit_seed(tmp_path: Path) -> None:
    root, manifest = _model_root(tmp_path)
    runtime = FakePathRuntime()
    loader = WarmOnceModelLoader(
        root=root,
        manifest=manifest,
        profile="cpu",
        factory=lambda _paths, _profile: runtime,
    )
    policy = _policy(manifest)
    worker = KronosWorker(
        policy=policy,
        loader=loader,
        clock=lambda: datetime(2026, 1, 9, 21, 1, tzinfo=UTC),
    )
    loader.warm()

    outcome = worker.forecast(_forecast_request(policy))

    assert outcome.error is None
    assert outcome.value is not None
    assert runtime.seeds == [17, 18, 19]
    assert tuple(path.seed for path in outcome.value.result.paths) == (17, 18, 19)
    assert outcome.value.result.input_volume_quality is VolumeQuality.OBSERVED
    assert outcome.value.result_artifact_hash == outcome.value.result.payload_hash()


def test_native_runtime_forces_single_sample_and_exact_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manifest = _model_root(tmp_path)
    policy = _policy(manifest)
    incoming = _forecast_request(policy)
    calls: list[dict[str, object]] = []
    seeded: list[int] = []

    class FakePrediction:
        def to_dict(self, *, orient: str) -> list[dict[str, float]]:
            assert orient == "records"
            return [
                {
                    "open": 101.0,
                    "high": 103.0,
                    "low": 100.0,
                    "close": 102.0,
                    "volume": 1001.0,
                    "amount": 102102.0,
                }
                for _ in incoming.future_timestamps
            ]

    class FakePredictor:
        def predict(self, _frame: object, **kwargs: object) -> FakePrediction:
            calls.append(kwargs)
            return FakePrediction()

    class FakeRandom:
        @staticmethod
        def seed(value: int) -> None:
            seeded.append(value)

    class FakeNumpy:
        random = FakeRandom()

    class FakeCuda:
        @staticmethod
        def manual_seed_all(value: int) -> None:
            seeded.append(value)

    class FakeTorch:
        cuda = FakeCuda()

        @staticmethod
        def manual_seed(value: int) -> None:
            seeded.append(value)

    class FakePandas:
        @staticmethod
        def Series(values: object) -> tuple[object, ...]:
            return tuple(values)  # type: ignore[arg-type]

        @staticmethod
        def DataFrame(rows: object, *, index: object) -> dict[str, object]:
            return {"rows": rows, "index": index}

    modules = {"torch": FakeTorch(), "numpy": FakeNumpy(), "pandas": FakePandas()}
    monkeypatch.setattr(
        model_loader_module.importlib, "import_module", lambda name: modules[name]
    )
    monkeypatch.setattr(model_loader_module.random, "seed", seeded.append)
    runtime = NativeKronosRuntime(
        model=object(),
        tokenizer=object(),
        predictor=FakePredictor(),
        profile="cpu",
    )

    points = runtime.predict_path(incoming, seed=17)

    assert seeded == [17, 17, 17]
    assert len(points) == incoming.horizon_bars
    assert calls[0]["sample_count"] == 1
    assert calls[0]["verbose"] is False


def test_forecast_fails_closed_before_or_after_inference_on_drift(
    tmp_path: Path,
) -> None:
    root, manifest = _model_root(tmp_path)
    runtime = FakePathRuntime()
    loader = WarmOnceModelLoader(
        root=root,
        manifest=manifest,
        profile="cpu",
        factory=lambda _paths, _profile: runtime,
    )
    policy = _policy(manifest)
    worker = KronosWorker(
        policy=policy,
        loader=loader,
        clock=lambda: datetime(2026, 1, 9, 21, 1, tzinfo=UTC),
    )
    loader.warm()
    mismatched = policy.runtime_identity.model_copy(update={"runtime_hash": "2" * 64})

    rejected = worker.forecast(_forecast_request(policy, runtime=mismatched))
    runtime.timestamp_drift = True
    invalid_output = worker.forecast(_forecast_request(policy))

    assert rejected.error is not None and rejected.error.code == "runtime_mismatch"
    assert runtime.seeds == [17]
    assert invalid_output.error is not None
    assert invalid_output.error.code == "invalid_model_output"


def test_forecast_rejects_expired_lease_without_running_model(tmp_path: Path) -> None:
    root, manifest = _model_root(tmp_path)
    runtime = FakePathRuntime()
    loader = WarmOnceModelLoader(
        root=root,
        manifest=manifest,
        profile="cpu",
        factory=lambda _paths, _profile: runtime,
    )
    policy = _policy(manifest)
    worker = KronosWorker(
        policy=policy,
        loader=loader,
        clock=lambda: datetime(2026, 1, 9, 21, 6, tzinfo=UTC),
    )
    loader.warm()

    outcome = worker.forecast(_forecast_request(policy))

    assert outcome.error is not None and outcome.error.code == "deadline_expired"
    assert runtime.seeds == []


def test_forecast_stops_between_seeded_paths_when_deadline_expires(
    tmp_path: Path,
) -> None:
    root, manifest = _model_root(tmp_path)
    runtime = FakePathRuntime()
    loader = WarmOnceModelLoader(
        root=root,
        manifest=manifest,
        profile="cpu",
        factory=lambda _paths, _profile: runtime,
    )
    policy = _policy(manifest)
    moments = iter(
        (
            datetime(2026, 1, 9, 21, 1, tzinfo=UTC),
            datetime(2026, 1, 9, 21, 1, tzinfo=UTC),
            datetime(2026, 1, 9, 21, 6, tzinfo=UTC),
        )
    )
    worker = KronosWorker(policy=policy, loader=loader, clock=lambda: next(moments))
    loader.warm()

    outcome = worker.forecast(_forecast_request(policy))

    assert outcome.error is not None and outcome.error.code == "deadline_expired"
    assert runtime.seeds == [17]


def test_http_liveness_readiness_and_bounded_preflight(tmp_path: Path) -> None:
    root, manifest = _model_root(tmp_path)
    loader = WarmOnceModelLoader(
        root=root,
        manifest=manifest,
        profile="cpu",
        factory=lambda _paths, _profile: object(),
    )
    worker = KronosWorker(policy=_policy(manifest), loader=loader)
    preflight_request = _request(manifest)
    client = TestClient(
        create_app(
            worker=worker,
            authenticator=ExactServiceAuthenticator.for_request(
                preflight_request,
                receiver=ServiceReceiver.KRONOS,
            ),
            max_request_bytes=4_096,
        )
    )

    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 503
    loader.warm()
    ready = client.get("/readyz")
    accepted = client.post(
        "/v1/preflight",
        json=preflight_request.model_dump(mode="json"),
        headers=authorization_headers(),
    )
    invalid = client.post(
        "/v1/preflight",
        content=b"{}",
        headers={**authorization_headers(), "content-type": "text/plain"},
    )
    oversized = client.post(
        "/v1/preflight",
        content=b"x" * 4_097,
        headers={**authorization_headers(), "content-type": "application/json"},
    )
    hostile_lengths = tuple(
        client.post(
            "/v1/preflight",
            content=b"{}",
            headers={
                **authorization_headers(),
                "content-length": declared,
                "content-type": "application/json",
            },
        )
        for declared in ("9" * 5_000, "not-a-number")
    )
    unauthenticated = client.post("/v1/preflight", json={})

    assert ready.status_code == 200 and ready.json()["data"]["ready"] is True
    assert accepted.status_code == 200 and accepted.json()["success"] is True
    assert invalid.status_code == 415
    assert oversized.status_code == 413
    assert all(response.status_code == 413 for response in hostile_lengths)
    assert all(
        response.json()["error"]["code"] == "request_too_large"
        for response in hostile_lengths
    )
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["www-authenticate"] == "Bearer"


def test_http_forecast_returns_lease_fenced_response(tmp_path: Path) -> None:
    root, manifest = _model_root(tmp_path)
    runtime = FakePathRuntime()
    loader = WarmOnceModelLoader(
        root=root,
        manifest=manifest,
        profile="cpu",
        factory=lambda _paths, _profile: runtime,
    )
    policy = _policy(manifest)
    worker = KronosWorker(
        policy=policy,
        loader=loader,
        clock=lambda: datetime(2026, 1, 9, 21, 1, tzinfo=UTC),
    )
    loader.warm()
    forecast_request = _forecast_request(policy)
    bound_authenticator = ExactServiceAuthenticator.for_request(
        forecast_request,
        receiver=ServiceReceiver.KRONOS,
    )
    client = TestClient(
        create_app(
            worker=worker,
            authenticator=bound_authenticator,
            max_request_bytes=65_536,
        )
    )

    response = client.post(
        "/v1/forecast",
        json=forecast_request.model_dump(mode="json"),
        headers=authorization_headers(),
    )
    denied = client.post(
        "/v1/forecast",
        json=forecast_request.model_dump(mode="json"),
        headers={"authorization": "Bearer wrong-but-long-service-token"},
    )
    wrong_target = TestClient(
        create_app(
            worker=worker,
            authenticator=ExactServiceAuthenticator.for_request(
                forecast_request,
                receiver=ServiceReceiver.KRONOS,
                target_identifier=UUID(int=999),
            ),
        )
    ).post(
        "/v1/forecast",
        json=forecast_request.model_dump(mode="json"),
        headers=authorization_headers(),
    )
    wrong_fence = TestClient(
        create_app(
            worker=worker,
            authenticator=bound_authenticator.altered(attempt_nonce_hash="f" * 64),
        )
    ).post(
        "/v1/forecast",
        json=forecast_request.model_dump(mode="json"),
        headers=authorization_headers(),
    )

    assert response.status_code == 200
    assert response.json()["data"]["attempt_nonce"] == "nonce-2"
    assert len(response.json()["data"]["result"]["paths"]) == 3
    assert denied.status_code == 401
    assert wrong_target.status_code == 403
    assert wrong_fence.status_code == 403
    assert runtime.seeds == [17, 18, 19]


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


def test_core_worker_config_runtime_hashes_match_selected_frozen_sources() -> None:
    for profile in ("cpu", "cuda"):
        payload = yaml.safe_load(
            (ROOT / "config" / "workers" / f"kronos_{profile}.yaml").read_text(
                encoding="utf-8"
            )
        )
        assert payload["runtime"]["runtime_hash"] == compute_runtime_hash(
            WORKER,
            profile,  # type: ignore[arg-type]
        )


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
