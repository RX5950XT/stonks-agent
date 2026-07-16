"""Fail-closed adapter around the pinned Qlib linear research runtime."""

from __future__ import annotations

import hashlib
import importlib
import logging
import platform
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stonks_contracts.common import stable_payload_hash
from stonks_contracts.quant_lab import (
    QuantBacktestPosition,
    QuantDatasetRow,
    QuantMetric,
    QuantPrediction,
    QuantResearchJob,
    QuantResearchResult,
    QuantRuntimeIdentity,
    QuantWorkerResponse,
)
from stonks_service_auth import service_auth_source_hash

LOGGER = logging.getLogger(__name__)
RUNTIME_FILES = (
    "Dockerfile",
    "app.py",
    "pyproject.toml",
    "qlib_adapter.py",
    "runtime_app.py",
    "uv.lock",
)


class WorkerError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: str = Field(min_length=1, max_length=256)


class WorkerFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error: WorkerError


class WorkerSuccess(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: QuantWorkerResponse


WorkerOutcome = WorkerSuccess | WorkerFailure


class RuntimeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    predictions: tuple[QuantPrediction, ...] = Field(min_length=1, max_length=1_000_000)
    model_parameters: tuple[Decimal, ...] = Field(min_length=1, max_length=128)
    warnings: tuple[str, ...] = Field(default=(), max_length=128)


class QuantRuntime(Protocol):
    identity: QuantRuntimeIdentity

    def fit_predict(self, job: QuantResearchJob) -> RuntimeOutput: ...


class WorkerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime: QuantRuntimeIdentity
    max_rows: int = Field(ge=2, le=1_000_000)


class QuantLabWorker:
    def __init__(
        self,
        *,
        policy: WorkerPolicy,
        runtime: QuantRuntime,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy = policy
        self.runtime = runtime
        self._clock = clock or (lambda: datetime.now(UTC))
        self._runtime_lock = Lock()

    def research(self, job: QuantResearchJob) -> WorkerOutcome:
        try:
            job = QuantResearchJob.model_validate(job.model_dump(mode="python"))
        except ValidationError:
            return _failure("invalid_job", "Quant research job is invalid")
        now = self._clock()
        if now.tzinfo is None or now >= job.deadline:
            return _failure("deadline_expired", "Quant research deadline expired")
        if (
            job.runtime != self.policy.runtime
            or self.runtime.identity != self.policy.runtime
        ):
            return _failure("runtime_mismatch", "Quant runtime identity mismatch")
        if len(job.dataset.rows) > self.policy.max_rows:
            return _failure("dataset_too_large", "Quant dataset exceeds row limit")
        split_rows = _split_rows(job)
        if split_rows is None:
            return _failure("split_invalid", "Quant split policy cannot be satisfied")
        try:
            with self._runtime_lock:
                output = self.runtime.fit_predict(job)
            finished_at = self._clock()
            if finished_at.tzinfo is None or finished_at >= job.deadline:
                return _failure("deadline_expired", "Quant research deadline expired")
            result = _build_result(job, output, split_rows[2], finished_at)
            response = QuantWorkerResponse(
                request_id=job.request_id,
                run_id=job.run_id,
                job_id=job.job_id,
                attempt_generation=job.attempt_generation,
                attempt_nonce=job.attempt_nonce,
                result_artifact_hash=result.payload_hash(),
                result=result,
            )
        except Exception as error:
            LOGGER.error(
                "quant research runtime failed error_type=%s",
                type(error).__name__,
            )
            return _failure("research_failed", "Quant research runtime failed")
        return WorkerSuccess(value=response)


class QlibLinearRuntime:
    """Pinned Qlib DataHandlerLP/DatasetH/LinearModel integration."""

    def __init__(self, identity: QuantRuntimeIdentity) -> None:
        self.identity = identity

    def fit_predict(self, job: QuantResearchJob) -> RuntimeOutput:
        qlib = importlib.import_module("qlib")
        numpy = importlib.import_module("numpy")
        pandas = importlib.import_module("pandas")
        sklearn = importlib.import_module("sklearn")
        self._validate_versions(qlib, numpy, pandas, sklearn)
        handler_module = importlib.import_module("qlib.data.dataset.handler")
        dataset_module = importlib.import_module("qlib.data.dataset")
        model_module = importlib.import_module("qlib.contrib.model.linear")
        frame = _qlib_frame(job, pandas)
        handler = handler_module.DataHandlerLP.from_df(frame)
        dataset = dataset_module.DatasetH(
            handler=handler,
            segments={
                "train": (job.split_policy.train_start, job.split_policy.train_end),
                "valid": (job.split_policy.valid_start, job.split_policy.valid_end),
                "test": (job.split_policy.test_start, job.split_policy.test_end),
            },
        )
        model = model_module.LinearModel(
            estimator="ols",
            fit_intercept=job.model_spec.fit_intercept,
            include_valid=False,
        )
        model.fit(dataset)
        scores = model.predict(dataset, segment="test")
        predictions = _qlib_predictions(job, scores)
        parameters = tuple(Decimal(str(value)) for value in model.coef_.tolist())
        return RuntimeOutput(
            predictions=predictions,
            model_parameters=parameters,
            warnings=(),
        )

    def _validate_versions(
        self, qlib: Any, numpy: Any, pandas: Any, sklearn: Any
    ) -> None:
        actual = (
            str(qlib.__version__),
            str(numpy.__version__),
            str(pandas.__version__),
            str(sklearn.__version__),
            platform.python_version(),
        )
        expected = (
            self.identity.qlib_version,
            self.identity.numpy_version,
            self.identity.pandas_version,
            self.identity.sklearn_version,
            self.identity.python_version,
        )
        if actual != expected:
            raise ValueError("installed quant runtime versions do not match identity")


def compute_runtime_hash(worker_root: Path) -> str:
    """Hash every execution-relevant worker file in a stable order."""

    identities = []
    for relative_path in RUNTIME_FILES:
        contents = (worker_root / relative_path).read_bytes()
        identities.append(
            {
                "path": relative_path,
                "sha256": hashlib.sha256(contents).hexdigest(),
            }
        )
    return stable_payload_hash(
        {
            "files": identities,
            "service_auth_source_hash": service_auth_source_hash(),
        }
    )


def _split_rows(
    job: QuantResearchJob,
) -> (
    tuple[
        tuple[QuantDatasetRow, ...],
        tuple[QuantDatasetRow, ...],
        tuple[QuantDatasetRow, ...],
    ]
    | None
):
    policy = job.split_policy
    rows = job.dataset.rows
    train = tuple(
        value
        for value in rows
        if policy.train_start <= value.event_at <= policy.train_end
    )
    valid = tuple(
        value
        for value in rows
        if policy.valid_start <= value.event_at <= policy.valid_end
    )
    test = tuple(
        value
        for value in rows
        if policy.test_start <= value.event_at <= policy.test_end
    )
    purge = sum(
        policy.train_end < value.event_at < policy.valid_start for value in rows
    )
    embargo = sum(
        policy.valid_end < value.event_at < policy.test_start for value in rows
    )
    if (
        not train
        or not valid
        or not test
        or purge < policy.purge_observations
        or embargo < policy.embargo_observations
        or any(value.label_available_at > policy.valid_start for value in train)
        or any(value.label_available_at > policy.test_start for value in valid)
    ):
        return None
    return train, valid, test


def _build_result(
    job: QuantResearchJob,
    output: RuntimeOutput,
    test_rows: tuple[QuantDatasetRow, ...],
    generated_at: datetime,
) -> QuantResearchResult:
    expected = tuple(
        (value.row_id, value.instrument_id, value.event_at, value.label)
        for value in test_rows
    )
    actual = tuple(
        (
            value.row_id,
            value.instrument_id,
            value.event_at,
            value.actual_return,
        )
        for value in output.predictions
    )
    if actual != expected:
        raise ValueError("runtime predictions do not align with test rows")
    positions = tuple(_position(value) for value in output.predictions)
    metrics = _metrics(output.predictions, positions, job)
    return QuantResearchResult(
        request_id=job.request_id,
        dataset_snapshot_id=job.dataset.dataset_snapshot_id,
        source_data_hash=job.dataset.source_data_hash,
        dataset_artifact_hash=job.dataset.payload_hash(),
        feature_spec_hash=job.feature_spec.spec_hash,
        label_spec_hash=job.label_spec.spec_hash,
        universe_spec_hash=job.universe_spec.spec_hash,
        cost_model_hash=job.cost_model.spec_hash,
        split_policy_hash=job.split_policy.spec_hash,
        model_spec_hash=job.model_spec.spec_hash,
        runtime=job.runtime,
        predictions=output.predictions,
        positions=positions,
        metrics=metrics,
        model_parameters=output.model_parameters,
        prediction_artifact_hash=_models_hash(output.predictions),
        position_artifact_hash=_models_hash(positions),
        metrics_artifact_hash=_models_hash(metrics),
        model_artifact_hash=stable_payload_hash(
            [str(value) for value in output.model_parameters]
        ),
        deterministic=True,
        generated_at=generated_at,
        warnings=output.warnings,
    )


def _position(prediction: QuantPrediction) -> QuantBacktestPosition:
    exposure = Decimal(0)
    if prediction.predicted_return > 0:
        exposure = Decimal(1)
    elif prediction.predicted_return < 0:
        exposure = Decimal(-1)
    return QuantBacktestPosition(
        row_id=prediction.row_id,
        instrument_id=prediction.instrument_id,
        event_at=prediction.event_at,
        research_exposure=exposure,
    )


def _metrics(
    predictions: tuple[QuantPrediction, ...],
    positions: tuple[QuantBacktestPosition, ...],
    job: QuantResearchJob,
) -> tuple[QuantMetric, ...]:
    count = Decimal(len(predictions))
    squared_error = (
        sum(
            (value.predicted_return - value.actual_return) ** 2 for value in predictions
        )
        / count
    )
    gross = tuple(
        position.research_exposure * prediction.actual_return
        for prediction, position in zip(predictions, positions, strict=True)
    )
    turnover = _turnover(positions)
    cost_rate = (job.cost_model.fee_bps + job.cost_model.slippage_bps) / Decimal(10_000)
    net = tuple(
        value - cost * cost_rate for value, cost in zip(gross, turnover, strict=True)
    )
    hits = sum(value > 0 for value in gross)
    return (
        QuantMetric(name="mean_squared_error", value=squared_error, unit="ratio"),
        QuantMetric(
            name="mean_gross_return",
            value=sum(gross, Decimal(0)) / count,
            unit="return",
        ),
        QuantMetric(
            name="mean_net_return",
            value=sum(net, Decimal(0)) / count,
            unit="return",
        ),
        QuantMetric(name="hit_rate", value=Decimal(hits) / count, unit="ratio"),
    )


def _turnover(positions: tuple[QuantBacktestPosition, ...]) -> tuple[Decimal, ...]:
    previous: dict[object, Decimal] = {}
    values: list[Decimal] = []
    for position in positions:
        before = previous.get(position.instrument_id, Decimal(0))
        values.append(abs(position.research_exposure - before))
        previous[position.instrument_id] = position.research_exposure
    return tuple(values)


def _qlib_frame(job: QuantResearchJob, pandas: Any) -> Any:
    index = pandas.MultiIndex.from_tuples(
        tuple((value.event_at, str(value.instrument_id)) for value in job.dataset.rows),
        names=("datetime", "instrument"),
    )
    columns = pandas.MultiIndex.from_tuples(
        (
            *(("feature", value.value) for value in job.feature_spec.names),
            ("label", "LABEL0"),
        )
    )
    data = tuple(
        (*(float(value) for value in row.features), float(row.label))
        for row in job.dataset.rows
    )
    return pandas.DataFrame(data, index=index, columns=columns)


def _qlib_predictions(
    job: QuantResearchJob, scores: Any
) -> tuple[QuantPrediction, ...]:
    score_map = {
        (timestamp.to_pydatetime(), UUID(instrument)): Decimal(str(score))
        for (timestamp, instrument), score in scores.items()
    }
    rows = tuple(
        value
        for value in job.dataset.rows
        if job.split_policy.test_start <= value.event_at <= job.split_policy.test_end
    )
    return tuple(
        QuantPrediction(
            row_id=value.row_id,
            instrument_id=value.instrument_id,
            event_at=value.event_at,
            predicted_return=score_map[(value.event_at, value.instrument_id)],
            actual_return=value.label,
        )
        for value in rows
    )


def _models_hash(values: tuple[BaseModel, ...]) -> str:
    return stable_payload_hash([value.model_dump(mode="json") for value in values])


def _failure(code: str, message: str) -> WorkerFailure:
    return WorkerFailure(error=WorkerError(code=code, message=message))
