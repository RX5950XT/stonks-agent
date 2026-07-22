#!/usr/bin/env python3
"""Run an isolated, bounded PostgreSQL logical backup/restore drill."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import stat
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import URL, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.sql.elements import TextClause

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.postgres_restore_contract import (
    DATABASE_NAME,
    DATABASE_USER,
    POSTGRES_IMAGE,
    REPORT_SCHEMA,
    SEED_SCHEMA,
    DrillBackend,
    DrillLimits,
    DumpProof,
    InstanceIdentity,
    InstancePair,
    RestoreDrillError,
    SeedProof,
    ValidationProof,
    canonical_event_hash,
    canonical_json,
    canonical_rows_digest,
    canonical_seed_events,
    execute_drill,
    postgres_container_command,
    replay_event_rows,
    validate_instance_pair,
)
from scripts.postgres_restore_process import (
    atomic_write as _atomic_write,
)
from scripts.postgres_restore_process import (
    optional_text as _optional_text,
)
from scripts.postgres_restore_process import run_bounded_input as _run_bounded_input
from scripts.postgres_restore_process import (
    run_bounded_output as _run_bounded_output,
)
from scripts.postgres_restore_process import (
    run_status as _run_status,
)
from scripts.postgres_restore_process import (
    run_text as _run_text,
)
from scripts.postgres_restore_process import (
    sha256_file as _sha256_file,
)

__all__ = (
    "POSTGRES_IMAGE",
    "DrillLimits",
    "DumpProof",
    "InstanceIdentity",
    "InstancePair",
    "RestoreDrillError",
    "SeedProof",
    "ValidationProof",
    "canonical_rows_digest",
    "canonical_seed_events",
    "execute_drill",
    "postgres_container_command",
    "replay_event_rows",
    "validate_instance_pair",
)

_ROLES = ("stonks_reader", "stonks_app", "stonks_worker")


class DockerPostgresBackend:
    """Isolated Docker/PostgreSQL implementation of the drill boundary."""

    def __init__(
        self,
        *,
        root: Path,
        workspace: Path,
        secret_file: Path,
        password: str,
        drill_id: str,
        limits: DrillLimits,
    ) -> None:
        self.root = root
        self.workspace = workspace
        self.secret_file = secret_file
        self.password = password
        self.drill_id = drill_id
        self.limits = limits
        suffix = drill_id[:12]
        self.network = f"stonks-drill-{suffix}"
        self.names = {
            "source": f"stonks-drill-{suffix}-source",
            "target": f"stonks-drill-{suffix}-target",
        }
        self.dump_path = workspace / "postgres.dump"
        self._owned_containers: set[str] = set()
        self._owned_network = False

    def setup(self) -> InstancePair:
        self.limits.validate()
        _run_text(
            ("docker", "info", "--format", "{{.ServerVersion}}"),
            timeout=30,
            secret=self.password,
        )
        self._assert_names_available()
        _run_text(
            ("docker", "pull", POSTGRES_IMAGE),
            timeout=self.limits.command_timeout_seconds,
            secret=self.password,
        )
        _run_text(
            (
                "docker",
                "network",
                "create",
                "--label",
                f"stonks-agent.restore-drill-id={self.drill_id}",
                self.network,
            ),
            timeout=30,
            secret=self.password,
        )
        if not self._network_is_owned():
            raise RestoreDrillError
        self._owned_network = True
        for role in ("source", "target"):
            created_id = _run_text(
                postgres_container_command(
                    name=self.names[role],
                    network=self.network,
                    secret_file=self.secret_file,
                    role=role,
                    drill_id=self.drill_id,
                ),
                timeout=self.limits.command_timeout_seconds,
                secret=self.password,
            ).strip()
            if len(created_id) != 64 or not self._container_is_owned(self.names[role]):
                raise RestoreDrillError
            self._owned_containers.add(self.names[role])
            self._wait_ready(self.names[role])
        pair = InstancePair(
            source=self._identity("source"),
            target=self._identity("target"),
        )
        validate_instance_pair(pair)
        return pair

    def migrate_and_seed(self, pair: InstancePair) -> SeedProof:
        engine = self._engine(pair.source.container_name)
        try:
            expected_head = self._migrate(engine)
            self._create_marker(engine, pair)
            self._insert_seed(engine)
            proof = self._query_proof(engine)
            if proof.schema_head != expected_head:
                raise RestoreDrillError
            return proof
        finally:
            engine.dispose()

    def dump(self, pair: InstancePair) -> DumpProof:
        _run_bounded_output(
            (
                "docker",
                "exec",
                pair.source.container_name,
                "pg_dump",
                "--format=custom",
                "--no-password",
                "--username",
                DATABASE_USER,
                "--dbname",
                DATABASE_NAME,
            ),
            output=self.dump_path,
            max_bytes=self.limits.max_dump_bytes,
            timeout=self.limits.command_timeout_seconds,
            secret=self.password,
        )
        size = self.dump_path.stat().st_size
        if size <= 0 or size > self.limits.max_dump_bytes:
            raise RestoreDrillError
        return DumpProof(size_bytes=size, sha256=_sha256_file(self.dump_path))

    def restore_and_validate(
        self,
        pair: InstancePair,
        source: SeedProof,
    ) -> ValidationProof:
        target_engine = self._engine(pair.target.container_name)
        try:
            self._create_restore_roles(target_engine)
        finally:
            target_engine.dispose()
        _run_bounded_input(
            _postgres_restore_command(pair.target.container_name),
            input_path=self.dump_path,
            timeout=self.limits.command_timeout_seconds,
            secret=self.password,
        )
        return self._validate_target(pair, source)

    def cleanup(self) -> None:
        failed = False
        for name in reversed(tuple(self._owned_containers)):
            if not self._container_is_owned(name):
                failed = True
                continue
            _run_status(("docker", "rm", "--force", name), timeout=30)
            if not self._container_exists(name):
                self._owned_containers.remove(name)
            else:
                failed = True
        if self._owned_network:
            if self._owned_containers or not self._network_is_owned():
                failed = True
            else:
                _run_status(("docker", "network", "rm", self.network), timeout=30)
                if not self._network_exists():
                    self._owned_network = False
                else:
                    failed = True
        if failed:
            raise RestoreDrillError

    def _assert_names_available(self) -> None:
        if any(self._container_exists(name) for name in self.names.values()):
            raise RestoreDrillError
        if self._network_exists():
            raise RestoreDrillError

    def _container_exists(self, name: str) -> bool:
        output = _run_text(
            (
                "docker",
                "container",
                "ls",
                "--all",
                "--filter",
                f"name=^/{name}$",
                "--format",
                "{{.Names}}",
            ),
            timeout=15,
            secret=self.password,
        )
        names = tuple(item for item in output.splitlines() if item)
        if any(item != name for item in names):
            raise RestoreDrillError
        return names == (name,)

    def _network_exists(self) -> bool:
        output = _run_text(
            (
                "docker",
                "network",
                "ls",
                "--filter",
                f"name=^{self.network}$",
                "--format",
                "{{.Name}}",
            ),
            timeout=15,
            secret=self.password,
        )
        names = tuple(item for item in output.splitlines() if item)
        if any(item != self.network for item in names):
            raise RestoreDrillError
        return names == (self.network,)

    def _container_is_owned(self, name: str) -> bool:
        role = next((key for key, value in self.names.items() if value == name), None)
        if role is None:
            return False
        value = _optional_text(
            (
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "stonks-agent.restore-drill-id"}}|'
                '{{index .Config.Labels "stonks-agent.restore-drill-role"}}',
                name,
            ),
            timeout=15,
        )
        return value is not None and value.strip() == f"{self.drill_id}|{role}"

    def _network_is_owned(self) -> bool:
        value = _optional_text(
            (
                "docker",
                "network",
                "inspect",
                "--format",
                '{{index .Labels "stonks-agent.restore-drill-id"}}',
                self.network,
            ),
            timeout=15,
        )
        return value is not None and value.strip() == self.drill_id

    def _validate_target(
        self,
        pair: InstancePair,
        source: SeedProof,
    ) -> ValidationProof:
        engine = self._engine(pair.target.container_name)
        try:
            restored = self._query_proof(engine)
            update_rejected = _mutation_rejected(
                engine,
                "update artifact_maintenance_event set reason='tampered' where sequence=1",
            )
            delete_rejected = _mutation_rejected(
                engine,
                "delete from artifact_maintenance_event where sequence=2",
            )
            chain_rejected = self._invalid_chain_rejected(engine)
            after_rejections = self._query_proof(engine)
            if (
                restored != after_rejections
                or restored.event_digest != source.event_digest
            ):
                raise RestoreDrillError
            return ValidationProof(
                schema_head=restored.schema_head,
                event_count=restored.event_count,
                event_digest=restored.event_digest,
                latest_occurred_at=restored.latest_occurred_at,
                append_only_update_rejected=update_rejected,
                append_only_delete_rejected=delete_rejected,
                invalid_chain_rejected=chain_rejected,
                source_marker_verified=self._verify_marker(engine, pair),
            )
        finally:
            engine.dispose()

    def _wait_ready(self, name: str) -> None:
        deadline = time.monotonic() + self.limits.startup_timeout_seconds
        command = (
            "docker",
            "exec",
            name,
            "pg_isready",
            "--username",
            DATABASE_USER,
            "--dbname",
            DATABASE_NAME,
        )
        while time.monotonic() < deadline:
            if _run_status(command, timeout=10) == 0:
                return
            time.sleep(0.25)
        raise RestoreDrillError

    def _identity(self, role: str) -> InstanceIdentity:
        name = self.names[role]
        raw = _run_text(
            ("docker", "inspect", "--format", "{{.Id}}|{{.Image}}", name),
            timeout=30,
            secret=self.password,
        ).strip()
        parts = raw.split("|")
        if len(parts) != 2:
            raise RestoreDrillError
        engine = self._engine(name)
        try:
            with engine.connect() as connection:
                system_id = connection.scalar(
                    text("select system_identifier from pg_control_system()")
                )
        finally:
            engine.dispose()
        return InstanceIdentity(
            role=role,
            container_name=name,
            container_id=parts[0],
            postgres_system_identifier=str(system_id),
            image_config_digest=parts[1],
        )

    def _engine(self, name: str) -> Engine:
        raw_ports = _run_text(
            (
                "docker",
                "inspect",
                "--format",
                "{{json .NetworkSettings.Ports}}",
                name,
            ),
            timeout=30,
            secret=self.password,
        ).strip()
        port = _published_port(raw_ports)
        url = URL.create(
            "postgresql+psycopg",
            username=DATABASE_USER,
            password=self.password,
            host="127.0.0.1",
            port=port,
            database=DATABASE_NAME,
        )
        return create_engine(
            url,
            pool_pre_ping=True,
            connect_args={
                "connect_timeout": min(
                    10,
                    int(self.limits.startup_timeout_seconds),
                )
            },
        )

    def _migrate(self, engine: Engine) -> str:
        config = Config(self.root / "alembic.ini")
        expected = ScriptDirectory.from_config(config).get_current_head()
        if expected is None:
            raise RestoreDrillError
        config.set_main_option("sqlalchemy.url", _alembic_url(engine.url))
        alembic_command.upgrade(config, "head")
        return expected

    def _create_marker(self, engine: Engine, pair: InstancePair) -> None:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "create table stonks_restore_drill_identity ("
                    "drill_id text primary key, "
                    "role text not null check (role='source'), "
                    "source_system_identifier text not null, "
                    "target_system_identifier text not null)"
                )
            )
            connection.execute(
                text(
                    "insert into stonks_restore_drill_identity "
                    "(drill_id, role, source_system_identifier, "
                    "target_system_identifier) values "
                    "(:drill_id, 'source', :source_id, :target_id)"
                ),
                {
                    "drill_id": self.drill_id,
                    "source_id": pair.source.postgres_system_identifier,
                    "target_id": pair.target.postgres_system_identifier,
                },
            )

    def _insert_seed(self, engine: Engine) -> None:
        statement = _event_insert_statement()
        for event in canonical_seed_events():
            values = dict(event)
            values["payload"] = json.dumps(values["payload"], sort_keys=True)
            with engine.begin() as connection:
                connection.execute(statement, values)
                connection.execute(
                    text(
                        "update artifact_maintenance_head set sequence=:sequence, "
                        "event_hash=:event_hash where head_id=1"
                    ),
                    {
                        "sequence": event["sequence"],
                        "event_hash": event["event_hash"],
                    },
                )

    def _query_proof(self, engine: Engine) -> SeedProof:
        with engine.connect() as connection:
            revision = connection.scalar(
                text("select version_num from alembic_version")
            )
            rows = [
                dict(row)
                for row in connection.execute(_event_select_statement()).mappings()
            ]
            head = connection.execute(
                text(
                    "select sequence, event_hash from artifact_maintenance_head "
                    "where head_id=1"
                )
            ).one()
        replay = replay_event_rows(rows, head=(int(head[0]), str(head[1])))
        if not isinstance(revision, str) or not revision:
            raise RestoreDrillError
        return SeedProof(
            schema_head=revision,
            event_count=replay.event_count,
            event_digest=replay.event_digest,
            latest_occurred_at=replay.latest_occurred_at,
        )

    def _create_restore_roles(self, engine: Engine) -> None:
        with engine.begin() as connection:
            for role in _ROLES:
                connection.execute(text(f"create role {role} nologin"))

    def _verify_marker(self, engine: Engine, pair: InstancePair) -> bool:
        with engine.connect() as connection:
            rows = [
                tuple(row)
                for row in connection.execute(
                    text(
                        "select drill_id, role, source_system_identifier, "
                        "target_system_identifier from stonks_restore_drill_identity"
                    )
                )
            ]
        return rows == [
            (
                self.drill_id,
                "source",
                pair.source.postgres_system_identifier,
                pair.target.postgres_system_identifier,
            )
        ]

    def _invalid_chain_rejected(self, engine: Engine) -> bool:
        invalid = dict(canonical_seed_events()[-1])
        invalid.update(
            {
                "event_id": "69000000-0000-4000-8000-000000000099",
                "operation_id": "69000000-0000-4000-8000-000000000099",
                "sequence": 3,
                "phase": "requested",
                "result_hash": None,
                "outcome": None,
                "previous_event_hash": "f" * 64,
                "payload": {"schema": SEED_SCHEMA, "step": "invalid_chain_probe"},
                "occurred_at": datetime(2026, 1, 2, 0, 0, 2, tzinfo=UTC),
            }
        )
        invalid["event_hash"] = canonical_event_hash(invalid)
        values = dict(invalid)
        values["payload"] = json.dumps(values["payload"], sort_keys=True)
        try:
            with engine.begin() as connection:
                connection.execute(_event_insert_statement(), values)
        except DBAPIError:
            return True
        return False


def _event_insert_statement() -> TextClause:
    return text(
        "insert into artifact_maintenance_event "
        "(event_id, operation_id, sequence, action, phase, content_hash, actor, "
        "reason, command_hash, result_hash, outcome, previous_event_hash, "
        "event_hash, payload, occurred_at) values "
        "(:event_id, :operation_id, :sequence, :action, :phase, :content_hash, "
        ":actor, :reason, :command_hash, :result_hash, :outcome, "
        ":previous_event_hash, :event_hash, cast(:payload as jsonb), :occurred_at)"
    )


def _event_select_statement() -> TextClause:
    return text(
        "select event_id, operation_id, sequence, action, phase, content_hash, "
        "actor, reason, command_hash, result_hash, outcome, previous_event_hash, "
        "event_hash, payload, occurred_at from artifact_maintenance_event "
        "order by sequence"
    )


def _mutation_rejected(engine: Engine, statement: str) -> bool:
    try:
        with engine.begin() as connection:
            connection.execute(text(statement))
    except DBAPIError:
        return True
    return False


def _alembic_url(url: URL) -> str:
    return url.render_as_string(hide_password=False).replace("%", "%%")


def _postgres_restore_command(container_name: str) -> tuple[str, ...]:
    if (
        re.fullmatch(r"stonks-drill-[a-z0-9-]{1,48}-(?:source|target)", container_name)
        is None
    ):
        raise RestoreDrillError
    return (
        "docker",
        "exec",
        "--interactive",
        container_name,
        "pg_restore",
        "--exit-on-error",
        "--single-transaction",
        "--no-owner",
        "--no-password",
        "--username",
        DATABASE_USER,
        "--dbname",
        DATABASE_NAME,
    )


def _published_port(raw: str) -> int:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RestoreDrillError from error
    if not isinstance(payload, dict) or set(payload) != {"5432/tcp"}:
        raise RestoreDrillError
    bindings = payload["5432/tcp"]
    if not isinstance(bindings, list) or len(bindings) != 1:
        raise RestoreDrillError
    binding = bindings[0]
    if not isinstance(binding, dict) or set(binding) != {"HostIp", "HostPort"}:
        raise RestoreDrillError
    host, raw_port = binding["HostIp"], binding["HostPort"]
    if host != "127.0.0.1" or not isinstance(raw_port, str) or not raw_port.isdecimal():
        raise RestoreDrillError
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise RestoreDrillError
    return port


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--startup-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--command-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--max-dump-bytes", type=int, default=128 * 1024 * 1024)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    limits = DrillLimits(
        startup_timeout_seconds=args.startup_timeout_seconds,
        command_timeout_seconds=args.command_timeout_seconds,
        max_dump_bytes=args.max_dump_bytes,
    )
    drill_id = secrets.token_hex(16)
    try:
        limits.validate()
        with tempfile.TemporaryDirectory(prefix="stonks-postgres-restore-") as raw:
            workspace = Path(raw)
            password = secrets.token_urlsafe(32)
            secret_file = workspace / "postgres-owner-password"
            secret_file.write_text(password, encoding="utf-8")
            secret_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
            backend: DrillBackend = DockerPostgresBackend(
                root=Path(__file__).resolve().parents[1],
                workspace=workspace,
                secret_file=secret_file,
                password=password,
                drill_id=drill_id,
                limits=limits,
            )
            report = execute_drill(backend, drill_id=drill_id)
        payload = canonical_json(report) + b"\n"
        if args.output is not None:
            _atomic_write(args.output, payload)
    except Exception:
        report = {
            "schema_version": REPORT_SCHEMA,
            "success": False,
            "status": "failed",
            "data": None,
            "error": {"code": "POSTGRES_RESTORE_DRILL_FAILED"},
        }
    print(json.dumps(report, ensure_ascii=True, allow_nan=False, sort_keys=True))
    return 0 if report["success"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
