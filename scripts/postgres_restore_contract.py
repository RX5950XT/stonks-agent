"""Pure contracts and proof validation for the PostgreSQL restore drill."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

POSTGRES_IMAGE = (
    "postgres:17.10-alpine@sha256:"
    "742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"
)
REPORT_SCHEMA = "stonks-agent/postgres-restore-drill/v1"
SEED_SCHEMA = "stonks-agent/postgres-restore-drill-seed/v1"
DATABASE_NAME = "stonks_restore"
DATABASE_USER = "postgres"
MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_DRILL_ID = re.compile(r"^[0-9a-f]{32}$")
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_EVENT_FIELDS = (
    "event_id",
    "operation_id",
    "sequence",
    "action",
    "phase",
    "content_hash",
    "actor",
    "reason",
    "command_hash",
    "result_hash",
    "outcome",
    "previous_event_hash",
    "payload",
    "occurred_at",
)


class RestoreDrillError(RuntimeError):
    """Fail-closed public error without infrastructure or secret detail."""

    def __init__(self) -> None:
        super().__init__("PostgreSQL restore drill failed")


@dataclass(frozen=True)
class DrillLimits:
    startup_timeout_seconds: float = 90.0
    command_timeout_seconds: float = 180.0
    max_dump_bytes: int = 128 * 1024 * 1024

    def validate(self) -> None:
        if not 1.0 <= self.startup_timeout_seconds <= 600.0:
            raise RestoreDrillError
        if not 1.0 <= self.command_timeout_seconds <= 3600.0:
            raise RestoreDrillError
        if not 1024 <= self.max_dump_bytes <= 1024 * 1024 * 1024:
            raise RestoreDrillError


@dataclass(frozen=True)
class InstanceIdentity:
    role: str
    container_name: str
    container_id: str
    postgres_system_identifier: str
    image_config_digest: str


@dataclass(frozen=True)
class InstancePair:
    source: InstanceIdentity
    target: InstanceIdentity


@dataclass(frozen=True)
class SeedProof:
    schema_head: str
    event_count: int
    event_digest: str
    latest_occurred_at: datetime


@dataclass(frozen=True)
class DumpProof:
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ValidationProof:
    schema_head: str
    event_count: int
    event_digest: str
    latest_occurred_at: datetime
    append_only_update_rejected: bool
    append_only_delete_rejected: bool
    invalid_chain_rejected: bool
    source_marker_verified: bool


class DrillBackend(Protocol):
    def setup(self) -> InstancePair: ...

    def migrate_and_seed(self, pair: InstancePair) -> SeedProof: ...

    def dump(self, pair: InstancePair) -> DumpProof: ...

    def restore_and_validate(
        self,
        pair: InstancePair,
        source: SeedProof,
    ) -> ValidationProof: ...

    def cleanup(self) -> None: ...


def postgres_container_command(
    *,
    name: str,
    network: str,
    secret_file: Path,
    role: str,
    drill_id: str,
) -> tuple[str, ...]:
    """Build a typed Docker argv with a file credential and exact image."""

    if (
        role not in {"source", "target"}
        or not _DRILL_ID.fullmatch(drill_id)
        or not _SAFE_NAME.fullmatch(name)
        or not _SAFE_NAME.fullmatch(network)
    ):
        raise RestoreDrillError
    mount = (
        f"type=bind,source={secret_file.resolve()},"
        "target=/run/secrets/postgres-owner-password,readonly"
    )
    return (
        "docker",
        "run",
        "--detach",
        "--name",
        name,
        "--network",
        network,
        "--publish",
        "127.0.0.1::5432",
        "--label",
        f"stonks-agent.restore-drill-id={drill_id}",
        "--label",
        f"stonks-agent.restore-drill-role={role}",
        "--env",
        f"POSTGRES_DB={DATABASE_NAME}",
        "--env",
        f"POSTGRES_USER={DATABASE_USER}",
        "--env",
        "POSTGRES_PASSWORD_FILE=/run/secrets/postgres-owner-password",
        "--env",
        "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=trust",
        "--env",
        "PGDATA=/var/lib/postgresql/data/pgdata",
        "--mount",
        mount,
        "--tmpfs",
        "/var/lib/postgresql/data:rw,nosuid,nodev,size=536870912",
        "--tmpfs",
        "/var/run/postgresql:rw,nosuid,nodev,size=16777216",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,size=67108864",
        POSTGRES_IMAGE,
    )


def canonical_seed_events() -> tuple[dict[str, object], ...]:
    common: dict[str, object] = {
        "operation_id": "69000000-0000-4000-8000-000000000001",
        "action": "restore",
        "content_hash": "a" * 64,
        "actor": "system:postgres-restore-drill",
        "reason": "p6.9-restore-verification",
        "command_hash": "b" * 64,
    }
    requested: dict[str, object] = {
        **common,
        "event_id": "69000000-0000-4000-8000-000000000011",
        "sequence": 1,
        "phase": "requested",
        "result_hash": None,
        "outcome": None,
        "previous_event_hash": None,
        "payload": {"schema": SEED_SCHEMA, "step": "backup_cutoff"},
        "occurred_at": datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
    }
    requested["event_hash"] = canonical_event_hash(requested)
    completed: dict[str, object] = {
        **common,
        "event_id": "69000000-0000-4000-8000-000000000012",
        "sequence": 2,
        "phase": "completed",
        "result_hash": "c" * 64,
        "outcome": "seed_committed",
        "previous_event_hash": requested["event_hash"],
        "payload": {"schema": SEED_SCHEMA, "step": "seed_committed"},
        "occurred_at": datetime(2026, 1, 2, 0, 0, 1, tzinfo=UTC),
    }
    completed["event_hash"] = canonical_event_hash(completed)
    return requested, completed


def canonical_event_hash(event: Mapping[str, object]) -> str:
    payload = {name: _json_value(event.get(name)) for name in _EVENT_FIELDS}
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def canonical_rows_digest(rows: Sequence[Mapping[str, object]]) -> str:
    normalized = [_normalize_event(item) for item in rows]
    return hashlib.sha256(canonical_json(normalized)).hexdigest()


def replay_event_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    head: tuple[int, str],
) -> SeedProof:
    expected = canonical_seed_events()
    normalized = tuple(_normalize_event(item) for item in rows)
    if len(normalized) != len(expected):
        raise RestoreDrillError
    previous: str | None = None
    for index, (actual, canonical) in enumerate(zip(normalized, expected, strict=True)):
        if actual != _normalize_event(canonical):
            raise RestoreDrillError
        if actual["sequence"] != index + 1:
            raise RestoreDrillError
        if actual["previous_event_hash"] != previous:
            raise RestoreDrillError
        if actual["event_hash"] != canonical_event_hash(actual):
            raise RestoreDrillError
        previous = str(actual["event_hash"])
    if head != (len(normalized), previous):
        raise RestoreDrillError
    latest = normalized[-1]["occurred_at"]
    if not isinstance(latest, datetime):
        raise RestoreDrillError
    return SeedProof(
        schema_head="",
        event_count=len(normalized),
        event_digest=canonical_rows_digest(normalized),
        latest_occurred_at=latest,
    )


def validate_instance_pair(pair: InstancePair) -> None:
    source, target = pair.source, pair.target
    values = (source, target)
    if source.role != "source" or target.role != "target":
        raise RestoreDrillError
    if any(
        not _SAFE_NAME.fullmatch(item.container_name)
        or not _CONTAINER_ID.fullmatch(item.container_id)
        or not item.postgres_system_identifier.isdecimal()
        or not _IMAGE_ID.fullmatch(item.image_config_digest)
        for item in values
    ):
        raise RestoreDrillError
    if (
        source.container_name == target.container_name
        or source.container_id == target.container_id
        or source.postgres_system_identifier == target.postgres_system_identifier
    ):
        raise RestoreDrillError


def execute_drill(
    backend: DrillBackend,
    *,
    drill_id: str,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    if not _DRILL_ID.fullmatch(drill_id):
        raise RestoreDrillError
    failure: Exception | None = None
    report: dict[str, object] | None = None
    try:
        pair = backend.setup()
        validate_instance_pair(pair)
        source = backend.migrate_and_seed(pair)
        dump = backend.dump(pair)
        restore_started = monotonic()
        target = backend.restore_and_validate(pair, source)
        rto_seconds = monotonic() - restore_started
        _validate_restore(source, target, dump, rto_seconds)
        report = _build_report(drill_id, pair, source, target, dump, rto_seconds)
    except Exception as error:
        failure = error
    try:
        backend.cleanup()
    except Exception as error:
        failure = error
    if failure is not None or report is None:
        raise RestoreDrillError from failure
    return report


def canonical_json(value: object) -> bytes:
    def default(item: object) -> str:
        if isinstance(item, datetime):
            return item.astimezone(UTC).isoformat().replace("+00:00", "Z")
        raise TypeError

    return json.dumps(
        value,
        default=default,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _normalize_event(event: Mapping[str, object]) -> dict[str, object]:
    normalized = {name: _json_value(event.get(name)) for name in _EVENT_FIELDS}
    normalized["event_hash"] = _json_value(event.get("event_hash"))
    if not isinstance(normalized["occurred_at"], datetime):
        raise RestoreDrillError
    for name in ("event_hash", "command_hash", "content_hash"):
        value = normalized[name]
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise RestoreDrillError
    return normalized


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise RestoreDrillError
        return value.astimezone(UTC)
    if hasattr(value, "hex") and type(value).__name__ == "UUID":
        return str(value)
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise RestoreDrillError
        return {key: _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (str, int)) or value is None:
        return value
    raise RestoreDrillError


def _validate_restore(
    source: SeedProof,
    target: ValidationProof,
    dump: DumpProof,
    rto_seconds: float,
) -> None:
    if (
        source.schema_head != target.schema_head
        or source.event_count != target.event_count
        or source.event_digest != target.event_digest
        or source.latest_occurred_at != target.latest_occurred_at
        or not target.append_only_update_rejected
        or not target.append_only_delete_rejected
        or not target.invalid_chain_rejected
        or not target.source_marker_verified
        or dump.size_bytes <= 0
        or not _DIGEST.fullmatch(dump.sha256)
        or not 0 <= rto_seconds <= 3600
    ):
        raise RestoreDrillError


def _build_report(
    drill_id: str,
    pair: InstancePair,
    source: SeedProof,
    target: ValidationProof,
    dump: DumpProof,
    rto_seconds: float,
) -> dict[str, object]:
    validation = asdict(target)
    validation["latest_occurred_at"] = target.latest_occurred_at.isoformat()
    return {
        "schema_version": REPORT_SCHEMA,
        "success": True,
        "status": "passed",
        "data": {
            "drill_id": drill_id,
            "image": {
                "reference": POSTGRES_IMAGE,
                "source_config_digest": pair.source.image_config_digest,
                "target_config_digest": pair.target.image_config_digest,
            },
            "identity": {
                "source_system_identifier": pair.source.postgres_system_identifier,
                "target_system_identifier": pair.target.postgres_system_identifier,
                "distinct": True,
            },
            "source": {
                "schema_head": source.schema_head,
                "event_count": source.event_count,
                "event_digest": source.event_digest,
                "latest_occurred_at": source.latest_occurred_at.isoformat(),
            },
            "backup": {
                "format": "postgresql-custom",
                "size_bytes": dump.size_bytes,
                "sha256": dump.sha256,
            },
            "validation": validation,
            "measurement": {
                "rto_seconds": round(rto_seconds, 6),
                "rpo_seconds": 0.0,
                "lost_events": 0,
                "basis": "seed_cutoff_to_restored_latest_event",
                "claim": "measured_drill_only_not_sla",
            },
        },
        "error": None,
    }
