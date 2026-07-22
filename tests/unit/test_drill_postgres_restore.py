from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import URL

from scripts import drill_postgres_restore as drill


def _identity(role: str, suffix: str) -> drill.InstanceIdentity:
    return drill.InstanceIdentity(
        role=role,
        container_name=f"stonks-drill-{role}",
        container_id=suffix * 64,
        postgres_system_identifier=str(int(suffix, 16) + 10_000),
        image_config_digest="sha256:" + suffix * 64,
    )


def _pair() -> drill.InstancePair:
    return drill.InstancePair(
        source=_identity("source", "a"),
        target=_identity("target", "b"),
    )


def _seed() -> drill.SeedProof:
    return drill.SeedProof(
        schema_head="0017",
        event_count=2,
        event_digest="c" * 64,
        latest_occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


def _validation() -> drill.ValidationProof:
    seed = _seed()
    return drill.ValidationProof(
        schema_head=seed.schema_head,
        event_count=seed.event_count,
        event_digest=seed.event_digest,
        latest_occurred_at=seed.latest_occurred_at,
        append_only_update_rejected=True,
        append_only_delete_rejected=True,
        invalid_chain_rejected=True,
        source_marker_verified=True,
    )


class FakeBackend:
    def __init__(self, *, validation: drill.ValidationProof | None = None) -> None:
        self.cleaned = False
        self.validation = validation or _validation()

    def setup(self) -> drill.InstancePair:
        return _pair()

    def migrate_and_seed(self, pair: drill.InstancePair) -> drill.SeedProof:
        assert pair == _pair()
        return _seed()

    def dump(self, pair: drill.InstancePair) -> drill.DumpProof:
        assert pair == _pair()
        return drill.DumpProof(size_bytes=1024, sha256="d" * 64)

    def restore_and_validate(
        self,
        pair: drill.InstancePair,
        source: drill.SeedProof,
    ) -> drill.ValidationProof:
        assert pair == _pair()
        assert source == _seed()
        return self.validation

    def cleanup(self) -> None:
        self.cleaned = True


def test_postgres_image_is_exactly_digest_pinned() -> None:
    assert drill.POSTGRES_IMAGE == (
        "postgres:17.10-alpine@sha256:"
        "742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"
    )


def test_container_command_uses_secret_file_without_secret_value(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "owner-password"
    command = drill.postgres_container_command(
        name="stonks-drill-source",
        network="stonks-drill-network",
        secret_file=secret,
        role="source",
        drill_id="a" * 32,
    )

    rendered = " ".join(command)
    assert command[0:3] == ("docker", "run", "--detach")
    assert command[-1] == drill.POSTGRES_IMAGE
    assert "POSTGRES_PASSWORD_FILE=/run/secrets/postgres-owner-password" in command
    assert "owner-secret-value" not in rendered
    assert "POSTGRES_PASSWORD=" not in rendered
    assert "127.0.0.1::5432" in command


@pytest.mark.parametrize(
    "limits",
    [
        drill.DrillLimits(startup_timeout_seconds=0),
        drill.DrillLimits(command_timeout_seconds=3601),
        drill.DrillLimits(max_dump_bytes=1023),
        drill.DrillLimits(max_dump_bytes=1024 * 1024 * 1024 + 1),
    ],
)
def test_limits_are_bounded(limits: drill.DrillLimits) -> None:
    with pytest.raises(drill.RestoreDrillError):
        limits.validate()


def test_canonical_seed_replays_exact_two_event_hash_chain() -> None:
    events = drill.canonical_seed_events()
    proof = drill.replay_event_rows(events, head=(2, str(events[-1]["event_hash"])))

    assert proof.event_count == 2
    assert events[0]["previous_event_hash"] is None
    assert events[1]["previous_event_hash"] == events[0]["event_hash"]
    assert proof.event_digest == drill.canonical_rows_digest(events)


def test_replay_rejects_tampering_chain_and_head() -> None:
    rows = [dict(item) for item in drill.canonical_seed_events()]
    rows[1]["payload"] = {"schema": "tampered"}

    with pytest.raises(drill.RestoreDrillError):
        drill.replay_event_rows(rows, head=(2, str(rows[1]["event_hash"])))

    events = drill.canonical_seed_events()
    with pytest.raises(drill.RestoreDrillError):
        drill.replay_event_rows(events, head=(1, str(events[-1]["event_hash"])))


def test_instance_identity_rejects_source_target_mixups() -> None:
    source = _identity("source", "a")
    target = _identity("target", "b")
    drill.validate_instance_pair(drill.InstancePair(source=source, target=target))

    with pytest.raises(drill.RestoreDrillError):
        drill.validate_instance_pair(drill.InstancePair(source=source, target=source))
    with pytest.raises(drill.RestoreDrillError):
        drill.validate_instance_pair(
            drill.InstancePair(
                source=source,
                target=drill.InstanceIdentity(
                    role="target",
                    container_name=target.container_name,
                    container_id=target.container_id,
                    postgres_system_identifier=source.postgres_system_identifier,
                    image_config_digest=target.image_config_digest,
                ),
            )
        )


def test_execute_drill_emits_measurements_not_sla_and_always_cleans() -> None:
    backend = FakeBackend()
    elapsed = iter((100.0, 101.25))

    report = drill.execute_drill(
        backend,
        drill_id="e" * 32,
        monotonic=lambda: next(elapsed),
    )

    assert backend.cleaned is True
    assert report["success"] is True
    data: dict[str, Any] = report["data"]
    assert data["measurement"] == {
        "rto_seconds": 1.25,
        "rpo_seconds": 0.0,
        "lost_events": 0,
        "basis": "seed_cutoff_to_restored_latest_event",
        "claim": "measured_drill_only_not_sla",
    }
    assert data["validation"]["append_only_update_rejected"] is True


def test_execute_drill_fails_closed_and_cleans_on_restore_mismatch() -> None:
    backend = FakeBackend(
        validation=drill.ValidationProof(
            **{
                **_validation().__dict__,
                "event_digest": "f" * 64,
            }
        )
    )

    with pytest.raises(drill.RestoreDrillError):
        drill.execute_drill(backend, drill_id="e" * 32)

    assert backend.cleaned is True


def test_alembic_url_keeps_in_memory_password_for_real_authentication() -> None:
    url = URL.create(
        "postgresql+psycopg",
        username="postgres",
        password="not-for-logs",
        host="127.0.0.1",
        port=5432,
        database="stonks_restore",
    )

    rendered = drill._alembic_url(url)

    assert "not-for-logs" in rendered
    assert "***" not in rendered


def test_cleanup_never_touches_unowned_or_label_mismatched_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = drill.DockerPostgresBackend(
        root=tmp_path,
        workspace=tmp_path,
        secret_file=tmp_path / "secret",
        password="not-for-argv",
        drill_id="a" * 32,
        limits=drill.DrillLimits(),
    )
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        drill,
        "_run_status",
        lambda command, **_: commands.append(tuple(command)) or 0,
    )

    backend.cleanup()

    assert commands == []
    backend._owned_containers.add(backend.names["source"])
    monkeypatch.setattr(backend, "_container_is_owned", lambda _: False)
    with pytest.raises(drill.RestoreDrillError):
        backend.cleanup()
    assert not any(command[:3] == ("docker", "rm", "--force") for command in commands)


def test_published_port_parser_requires_one_loopback_binding() -> None:
    payload = json.dumps({"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55432"}]})

    assert drill._published_port(payload) == 55432
    for invalid in (
        "null",
        "{}",
        json.dumps({"5432/tcp": None}),
        json.dumps({"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "55432"}]}),
        json.dumps({"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "0"}]}),
    ):
        with pytest.raises(drill.RestoreDrillError):
            drill._published_port(invalid)


def test_restore_streams_archive_over_stdin_without_container_path() -> None:
    command = drill._postgres_restore_command("stonks-drill-abc-target")

    assert command[:4] == (
        "docker",
        "exec",
        "--interactive",
        "stonks-drill-abc-target",
    )
    assert "pg_restore" in command
    assert "/tmp/restore.dump" not in command
    assert command[-2:] == ("--dbname", "stonks_restore")
