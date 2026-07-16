"""Pinned local-only Kronos model validation and warm-once loading."""

from __future__ import annotations

import hashlib
import importlib
import json
import random
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_contracts.common import stable_payload_hash
from stonks_contracts.kronos import (
    KronosForecastPoint,
    KronosWorkerRequest,
)
from stonks_service_auth import service_auth_source_hash

type DeviceProfile = Literal["cpu", "cuda"]
type RuntimeFactory = Callable[["ValidatedModelPaths", DeviceProfile], object]


class ModelLoadError(RuntimeError):
    """A fail-closed model validation or startup error."""


class ModelFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ModelComponent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component_id: str = Field(pattern=r"^Kronos(?:-Tokenizer)?-[A-Za-z0-9-]+$")
    repository: str = Field(pattern=r"^NeoQuasar/Kronos(?:-Tokenizer)?-[A-Za-z0-9-]+$")
    revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    directory: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    files: tuple[ModelFile, ...] = Field(min_length=2, max_length=16)

    @model_validator(mode="after")
    def validate_files(self) -> Self:
        names = tuple(item.path for item in self.files)
        if len(names) != len(set(names)):
            raise ValueError("model component file paths must be unique")
        if set(names) != {"config.json", "model.safetensors"}:
            raise ValueError("model component files must be exact")
        return self


class KronosModelManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_version: Literal["1.0.0"]
    upstream_repository: Literal["https://github.com/shiyu-coder/Kronos"]
    upstream_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_archive_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    max_context: int = Field(ge=1, le=2_048)
    model: ModelComponent
    tokenizer: ModelComponent

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        if self.model.directory == self.tokenizer.directory:
            raise ValueError("model component directories must differ")
        return self

    def payload_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json"))


@dataclass(frozen=True, slots=True)
class ValidatedModelPaths:
    root: Path
    model_dir: Path
    tokenizer_dir: Path


def load_model_manifest(path: Path) -> KronosModelManifest:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
        return KronosModelManifest.model_validate(payload)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ModelLoadError("model manifest is invalid") from error


def compute_runtime_hash(worker_root: Path, profile: DeviceProfile) -> str:
    """Hash worker source and the selected frozen dependency profile."""
    selected = [
        worker_root / name
        for name in (
            "adapter.py",
            "app.py",
            "model_loader.py",
            "runtime_app.py",
            "model-manifest.json",
        )
    ]
    profile_root = (
        worker_root if profile == "cpu" else worker_root / "profiles" / "cuda"
    )
    selected.extend((profile_root / "pyproject.toml", profile_root / "uv.lock"))
    payload: list[dict[str, str]] = []
    for path in selected:
        try:
            content = path.read_bytes()
        except OSError as error:
            raise ModelLoadError("runtime identity file is unavailable") from error
        payload.append(
            {
                "path": path.relative_to(worker_root).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return stable_payload_hash(
        {
            "profile": profile,
            "files": payload,
            "service_auth_source_hash": service_auth_source_hash(),
        }
    )


def validate_model_root(
    root: Path, manifest: KronosModelManifest
) -> ValidatedModelPaths:
    if root.is_symlink():
        raise ModelLoadError("model root must not be a symlink")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise ModelLoadError("model root is unavailable") from error
    if not resolved_root.is_dir():
        raise ModelLoadError("model root is not a directory")
    directories: dict[str, Path] = {}
    for name, component in (
        ("model", manifest.model),
        ("tokenizer", manifest.tokenizer),
    ):
        directory = _validate_component(resolved_root, component)
        directories[name] = directory
    expected_directories = {manifest.model.directory, manifest.tokenizer.directory}
    actual_directories = {item.name for item in resolved_root.iterdir()}
    if actual_directories != expected_directories:
        raise ModelLoadError("model root contains untracked entries")
    return ValidatedModelPaths(
        root=resolved_root,
        model_dir=directories["model"],
        tokenizer_dir=directories["tokenizer"],
    )


def _validate_component(root: Path, component: ModelComponent) -> Path:
    directory = root / component.directory
    if directory.is_symlink():
        raise ModelLoadError("model component directory must not be a symlink")
    try:
        resolved = directory.resolve(strict=True)
    except OSError as error:
        raise ModelLoadError("model component directory is unavailable") from error
    if resolved.parent != root or not resolved.is_dir():
        raise ModelLoadError("model component directory escapes model root")
    expected = {item.path for item in component.files}
    actual = {item.name for item in resolved.iterdir()}
    if actual != expected:
        raise ModelLoadError("model component files do not match manifest")
    for expected_file in component.files:
        _validate_file(resolved / expected_file.path, expected_file)
    return resolved


def _validate_file(path: Path, expected: ModelFile) -> None:
    if path.is_symlink():
        raise ModelLoadError("model file must not be a symlink")
    try:
        stat = path.stat()
    except OSError as error:
        raise ModelLoadError("model file is unavailable") from error
    if not path.is_file() or stat.st_size != expected.size_bytes:
        raise ModelLoadError("model file size mismatch")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1_048_576), b""):
                digest.update(chunk)
    except OSError as error:
        raise ModelLoadError("model file could not be read") from error
    if digest.hexdigest() != expected.sha256:
        raise ModelLoadError("model file checksum mismatch")


class WarmOnceModelLoader:
    """Validate and load at startup; requests may only read the warmed runtime."""

    def __init__(
        self,
        *,
        root: Path,
        manifest: KronosModelManifest,
        profile: DeviceProfile,
        factory: RuntimeFactory,
    ) -> None:
        self.manifest = manifest
        self._root = root
        self._profile = profile
        self._factory = factory
        self._lock = Lock()
        self._runtime: object | None = None
        self._failure: ModelLoadError | None = None
        self._attempted = False

    @property
    def ready(self) -> bool:
        return self._runtime is not None

    def warm(self) -> object:
        with self._lock:
            if self._runtime is not None:
                return self._runtime
            if self._failure is not None:
                raise self._failure
            if self._attempted:
                raise ModelLoadError("startup model load failed")
            self._attempted = True
            try:
                paths = validate_model_root(self._root, self.manifest)
                self._runtime = self._factory(paths, self._profile)
            except Exception as error:
                self._failure = ModelLoadError("startup model load failed")
                raise self._failure from error
            return self._runtime

    def get(self) -> object:
        if self._runtime is None:
            raise ModelLoadError("model is not warmed")
        return self._runtime


@dataclass(frozen=True, slots=True)
class NativeKronosRuntime:
    model: Any
    tokenizer: Any
    predictor: Any
    profile: DeviceProfile

    def predict_path(
        self, request: KronosWorkerRequest, *, seed: int
    ) -> tuple[KronosForecastPoint, ...]:
        """Run exactly one seeded sample so no upstream path averaging occurs."""
        torch = importlib.import_module("torch")
        numpy = importlib.import_module("numpy")
        pandas = importlib.import_module("pandas")
        torch.manual_seed(seed)
        numpy.random.seed(seed % 2**32)
        random.seed(seed)
        if self.profile == "cuda":
            torch.cuda.manual_seed_all(seed)
        rows = [
            {
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume or 0),
                "amount": float(bar.amount or 0),
            }
            for bar in request.bars
        ]
        x_timestamp = pandas.Series([bar.event_time for bar in request.bars])
        y_timestamp = pandas.Series(request.future_timestamps)
        frame = pandas.DataFrame(rows, index=x_timestamp)
        prediction = self.predictor.predict(
            frame,
            x_timestamp=x_timestamp,
            y_timestamp=y_timestamp,
            pred_len=request.horizon_bars,
            T=float(request.sampling.temperature),
            top_k=request.sampling.top_k,
            top_p=float(request.sampling.top_p),
            sample_count=1,
            verbose=False,
        )
        records = prediction.to_dict(orient="records")
        if len(records) != request.horizon_bars:
            raise ModelLoadError("Kronos output length mismatch")
        return tuple(
            KronosForecastPoint(
                timestamp=timestamp,
                open=Decimal(str(record["open"])),
                high=Decimal(str(record["high"])),
                low=Decimal(str(record["low"])),
                close=Decimal(str(record["close"])),
                volume=Decimal(str(record["volume"])),
                amount=Decimal(str(record["amount"])),
            )
            for timestamp, record in zip(
                request.future_timestamps, records, strict=True
            )
        )


def create_native_runtime(
    paths: ValidatedModelPaths, profile: DeviceProfile
) -> NativeKronosRuntime:
    """Dynamically import heavy dependencies only inside the isolated worker."""
    try:
        torch = importlib.import_module("torch")
        module = importlib.import_module("model")
    except ImportError as error:
        raise ModelLoadError("Kronos runtime dependencies are unavailable") from error
    if profile == "cuda" and not bool(torch.cuda.is_available()):
        raise ModelLoadError("CUDA profile requires an available CUDA device")
    device = "cuda" if profile == "cuda" else "cpu"
    try:
        tokenizer = module.KronosTokenizer.from_pretrained(str(paths.tokenizer_dir))
        model = module.Kronos.from_pretrained(str(paths.model_dir))
        tokenizer.eval()
        model.eval()
        predictor = module.KronosPredictor(
            model,
            tokenizer,
            device=device,
            max_context=512,
        )
    except Exception as error:
        raise ModelLoadError("local Kronos weights could not be loaded") from error
    return NativeKronosRuntime(
        model=model,
        tokenizer=tokenizer,
        predictor=predictor,
        profile=profile,
    )
