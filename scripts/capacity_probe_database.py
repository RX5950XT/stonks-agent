"""Exact-scope PostgreSQL capacity workloads and evidence verification."""

from __future__ import annotations

from datetime import timedelta
from threading import Lock
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import URL, Engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from scripts.capacity_probe_common import (
    EXPECTED_SCHEMA_REVISION,
    FIXED_NOW,
    ProbeError,
)
from stonks_agent.adapters.postgres.job_queue import PostgresJobQueue
from stonks_agent.adapters.postgres.models import (
    ArtifactManifestRow,
    DatasetSnapshotRow,
    JobRow,
    RunDatasetSnapshotRow,
    WorkflowRunRow,
)
from stonks_agent.adapters.postgres.research_query import PostgresResearchRequestStore
from stonks_agent.adapters.postgres.snapshot_requests import (
    PostgresSnapshotRequestStore,
)
from stonks_agent.domain.errors import Success
from stonks_agent.domain.job import EnqueueJob
from stonks_agent.domain.research_run import ResearchRunRequest
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_agent.domain.workflow import WorkflowStatus
from stonks_contracts.common import stable_payload_hash

DATABASE_NAME = "stonks_capacity"
_DATABASE_HOSTS = frozenset({"127.0.0.1", "::1"})


def validate_capacity_database_url(value: str) -> URL:
    try:
        parsed = make_url(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise ProbeError("capacity database configuration is invalid") from None
    valid = (
        parsed.drivername == "postgresql+psycopg"
        and parsed.database == DATABASE_NAME
        and parsed.host in _DATABASE_HOSTS
        and parsed.username is not None
        and bool(parsed.username)
        and parsed.password is None
        and not parsed.query
        and (port is None or 1 <= port <= 65_535)
    )
    if not valid:
        raise ProbeError("capacity database configuration is invalid")
    return parsed


class DatabaseProbe:
    """Exact-scope transactions against the dedicated disposable capacity DB."""

    def __init__(self, engine: Engine, *, identity: str) -> None:
        if (
            len(identity) != 16
            or not identity.isascii()
            or any(character not in "0123456789abcdef" for character in identity)
        ):
            raise ProbeError("probe identity is invalid")
        self._engine = engine
        self._identity = identity
        self._owner = f"system:capacity-probe:{identity}"
        self._policy = f"capacity-probe/{identity}"
        self._snapshot_id = uuid5(NAMESPACE_URL, f"stonks:capacity:{identity}:snapshot")
        self._artifact_hash = stable_payload_hash(
            {"capacity_probe": identity, "kind": "snapshot_manifest"}
        )
        self._run_ids: set[UUID] = set()
        self._guard = Lock()

    def prepare(self) -> None:
        try:
            with self._engine.begin() as connection:
                database = connection.scalar(text("select current_database()"))
                revisions = tuple(
                    connection.execute(
                        text("select version_num from alembic_version")
                    ).scalars()
                )
                canonical_rows = connection.scalar(
                    text(
                        "select "
                        "(select count(*) from run) + "
                        "(select count(*) from job) + "
                        "(select count(*) from dataset_snapshot) + "
                        "(select count(*) from run_dataset_snapshot) + "
                        "(select count(*) from artifact_manifest)"
                    )
                )
            if (
                database != DATABASE_NAME
                or revisions != (EXPECTED_SCHEMA_REVISION,)
                or canonical_rows != 0
            ):
                raise ProbeError("capacity database schema is invalid")
            self.verify_evidence_scope()
            self._seed_snapshot()
        except ProbeError:
            raise
        except SQLAlchemyError:
            raise ProbeError("capacity database preparation failed") from None

    def queue_once(self, index: int) -> str:
        run_id = self._run_id("queue", index)
        self._track(run_id)
        now = FIXED_NOW + timedelta(seconds=index)
        payload = {"schema": "capacity-queue/1", "sample": index}
        try:
            with Session(self._engine) as session, session.begin():
                session.add(
                    WorkflowRunRow(
                        run_id=run_id,
                        run_type="capacity_queue",
                        status=WorkflowStatus.PENDING.value,
                        as_of=now,
                        policy_id=self._policy,
                        idempotency_key=f"capacity:{self._identity}:queue:{index}:run",
                        input_hash=stable_payload_hash(payload),
                        owner_subject=self._owner_for("queue"),
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
            job_id = uuid5(run_id, "job")
            result = PostgresJobQueue(self._engine).enqueue(
                EnqueueJob(
                    job_id=job_id,
                    run_id=run_id,
                    job_type="capacity_probe",
                    payload=payload,
                    idempotency_key=f"capacity:{self._identity}:queue:{index}:job",
                    not_before=now,
                    deadline_at=now + timedelta(minutes=5),
                    max_attempts=1,
                    created_at=now,
                )
            )
            if not isinstance(result, Success) or result.value.job_id != job_id:
                raise ProbeError("queue transaction validation failed")
            self._require_exact_graph(run_id, job_id, snapshot_id=None)
            return stable_payload_hash({"run_id": str(run_id), "job_id": str(job_id)})
        except ProbeError:
            raise
        except (SQLAlchemyError, ValueError):
            raise ProbeError("queue transaction failed") from None

    def snapshot_once(self, index: int) -> str:
        now = FIXED_NOW + timedelta(seconds=index)
        request = CreateSnapshotRequest(
            market="US",
            capability="daily_bars",
            as_of=now,
            query={"symbol": "AAPL", "sample": index},
            provider_policy_id=self._policy,
            idempotency_key=f"capacity-snapshot-{self._identity}-{index}",
            owner_subject=self._owner_for("snapshot"),
            requested_at=now,
        )
        try:
            result = PostgresSnapshotRequestStore(self._engine).submit(request)
            if not isinstance(result, Success):
                raise ProbeError("snapshot transaction validation failed")
            self._track(result.value.run_id)
            self._require_exact_graph(
                result.value.run_id,
                result.value.job_id,
                snapshot_id=None,
            )
            return stable_payload_hash(result.value.model_dump(mode="json"))
        except ProbeError:
            raise
        except (SQLAlchemyError, ValueError):
            raise ProbeError("snapshot transaction failed") from None

    def research_once(self, index: int) -> str:
        now = FIXED_NOW + timedelta(seconds=index)
        request = ResearchRunRequest(
            instrument_id="instrument-aapl",
            symbol="AAPL",
            as_of=FIXED_NOW,
            snapshot_id=self._snapshot_id,
            research_profile_id=self._policy,
            model_policy_id="capacity-model/1",
            language="zh-TW",
            idempotency_key=f"capacity-research-{self._identity}-{index}",
            owner_subject=self._owner_for("research"),
            requested_at=now,
        )
        try:
            result = PostgresResearchRequestStore(self._engine).submit(request)
            if not isinstance(result, Success):
                raise ProbeError("research transaction validation failed")
            self._track(result.value.run_id)
            self._require_exact_graph(
                result.value.run_id,
                result.value.job_id,
                snapshot_id=self._snapshot_id,
            )
            return stable_payload_hash(result.value.model_dump(mode="json"))
        except ProbeError:
            raise
        except (SQLAlchemyError, ValueError):
            raise ProbeError("research transaction failed") from None

    def verify_evidence_scope(self) -> None:
        """Verify exact scope; append-only canonical rows are DB-lifetime evidence."""
        with self._guard:
            run_ids = frozenset(self._run_ids)
        try:
            with Session(self._engine) as session:
                runs = tuple(session.scalars(select(WorkflowRunRow)))
                jobs = tuple(session.scalars(select(JobRow)))
                links = tuple(session.scalars(select(RunDatasetSnapshotRow)))
                snapshots = tuple(session.scalars(select(DatasetSnapshotRow)))
                artifacts = tuple(session.scalars(select(ArtifactManifestRow)))
            if not self._scope_is_exact(
                run_ids,
                runs=runs,
                jobs=jobs,
                links=links,
                snapshots=snapshots,
                artifacts=artifacts,
            ):
                raise ProbeError("capacity evidence graph is not exact")
        except ProbeError:
            raise
        except SQLAlchemyError:
            raise ProbeError("capacity evidence verification failed") from None

    def _scope_is_exact(
        self,
        run_ids: frozenset[UUID],
        *,
        runs: tuple[WorkflowRunRow, ...],
        jobs: tuple[JobRow, ...],
        links: tuple[RunDatasetSnapshotRow, ...],
        snapshots: tuple[DatasetSnapshotRow, ...],
        artifacts: tuple[ArtifactManifestRow, ...],
    ) -> bool:
        persisted_run_ids = {row.run_id for row in runs}
        job_run_ids = {row.run_id for row in jobs}
        research_run_ids = {
            row.run_id for row in runs if row.run_type == "research_report"
        }
        link_run_ids = {row.run_id for row in links}
        seed_absent = not snapshots and not artifacts
        seed_exact = (
            len(snapshots) == 1
            and snapshots[0].snapshot_id == self._snapshot_id
            and snapshots[0].provider_policy_id == self._policy
            and snapshots[0].manifest_artifact_hash == self._artifact_hash
            and len(artifacts) == 1
            and artifacts[0].content_hash == self._artifact_hash
            and artifacts[0].source == "stonks-capacity-probe"
        )
        return all(
            (
                persisted_run_ids == run_ids,
                len(runs) == len(run_ids),
                all(row.owner_subject.startswith(self._owner) for row in runs),
                job_run_ids == run_ids,
                len(jobs) == len(run_ids),
                all(
                    job.payload_hash == stable_payload_hash(job.payload) for job in jobs
                ),
                link_run_ids == research_run_ids,
                len(links) == len(research_run_ids),
                all(link.snapshot_id == self._snapshot_id for link in links),
                seed_absent or seed_exact,
            )
        )

    def _seed_snapshot(self) -> None:
        metadata: dict[str, object] = {
            "media_type": "application/json",
            "license_tag": "Apache-2.0",
            "sensitivity": "internal",
            "source": "stonks-capacity-probe",
            "attributes": [["schema", "capacity-snapshot/1"]],
        }
        with Session(self._engine) as session, session.begin():
            session.add(
                ArtifactManifestRow(
                    content_hash=self._artifact_hash,
                    size_bytes=2,
                    media_type="application/json",
                    license_tag="Apache-2.0",
                    sensitivity="internal",
                    source="stonks-capacity-probe",
                    finalized_at=FIXED_NOW,
                    storage_uri=f"memory://capacity/{self._artifact_hash}",
                    metadata_payload=metadata,
                )
            )
            session.add(
                DatasetSnapshotRow(
                    snapshot_id=self._snapshot_id,
                    as_of=FIXED_NOW,
                    cutoff_at=FIXED_NOW,
                    provider_policy_id=self._policy,
                    manifest_artifact_hash=self._artifact_hash,
                    content_hash=stable_payload_hash(
                        {"snapshot": str(self._snapshot_id)}
                    ),
                    manifest={"schema": "capacity-snapshot/1"},
                    created_at=FIXED_NOW,
                )
            )

    def _require_exact_graph(
        self,
        run_id: UUID,
        job_id: UUID,
        *,
        snapshot_id: UUID | None,
    ) -> None:
        with Session(self._engine) as session:
            run = session.get(WorkflowRunRow, run_id)
            job = session.get(JobRow, job_id)
            link = session.get(RunDatasetSnapshotRow, run_id)
        valid = (
            run is not None
            and run.owner_subject.startswith(self._owner)
            and run.policy_id == self._policy
            and job is not None
            and job.run_id == run_id
            and job.payload_hash == stable_payload_hash(job.payload)
            and (
                (snapshot_id is None and link is None)
                or (
                    snapshot_id is not None
                    and link is not None
                    and link.snapshot_id == snapshot_id
                )
            )
        )
        if not valid:
            raise ProbeError("database transaction binding validation failed")

    def _run_id(self, workload: str, index: int) -> UUID:
        if type(index) is not int or index < 0:
            raise ProbeError("sample identity is invalid")
        return uuid5(
            NAMESPACE_URL,
            f"stonks:capacity:{self._identity}:{workload}:{index}:run",
        )

    def _owner_for(self, workload: str) -> str:
        return f"{self._owner}:{workload}"

    def _track(self, run_id: UUID) -> None:
        with self._guard:
            if run_id in self._run_ids:
                raise ProbeError("capacity sample identity was reused")
            self._run_ids.add(run_id)
