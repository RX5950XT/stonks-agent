from __future__ import annotations

import os
import secrets
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from scripts.capacity_probe_common import ProbeError
from scripts.capacity_probe_database import (
    DatabaseProbe,
    validate_capacity_database_url,
)
from stonks_agent.config.capacity import load_capacity_policy
from stonks_agent.domain.capacity import CapacityWorkload

pytestmark = [
    pytest.mark.performance,
    pytest.mark.integration,
    pytest.mark.postgres,
]


def _capacity_url() -> str:
    value = os.environ.get("STONKS_CAPACITY_DATABASE_URL")
    if value is None:
        pytest.skip("STONKS_CAPACITY_DATABASE_URL is not configured")
    validate_capacity_database_url(value)
    return value


def test_actual_postgres_primitives_use_database_lifetime_isolation() -> None:
    engine = create_engine(_capacity_url(), pool_pre_ping=True)
    identity = secrets.token_hex(8)
    probe = DatabaseProbe(engine, identity=identity)
    policy = load_capacity_policy(
        Path(__file__).resolve().parents[2] / "config" / "capacity.yaml"
    )
    expected_counts = {
        workload: policy.definition_for(workload).sample_count
        for workload in (
            CapacityWorkload.QUEUE,
            CapacityWorkload.SNAPSHOT,
            CapacityWorkload.RESEARCH,
        )
    }

    try:
        probe.prepare()
        queue_hashes = {
            probe.queue_once(index)
            for index in range(expected_counts[CapacityWorkload.QUEUE])
        }
        snapshot_hashes = {
            probe.snapshot_once(index)
            for index in range(expected_counts[CapacityWorkload.SNAPSHOT])
        }
        research_hashes = {
            probe.research_once(index)
            for index in range(expected_counts[CapacityWorkload.RESEARCH])
        }

        assert len(queue_hashes) == expected_counts[CapacityWorkload.QUEUE]
        assert len(snapshot_hashes) == expected_counts[CapacityWorkload.SNAPSHOT]
        assert len(research_hashes) == expected_counts[CapacityWorkload.RESEARCH]
    finally:
        probe.verify_evidence_scope()
        with engine.connect() as connection:
            counts = connection.execute(
                text(
                    "select "
                    "(select count(*) from run where owner_subject like :scope) runs, "
                    "(select count(*) from dataset_snapshot where "
                    "provider_policy_id=:policy) snapshots"
                ),
                {
                    "scope": f"system:capacity-probe:{identity}%",
                    "policy": f"capacity-probe/{identity}",
                },
            ).one()
        with pytest.raises(ProbeError, match="capacity database schema is invalid"):
            DatabaseProbe(engine, identity=secrets.token_hex(8)).prepare()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "insert into run "
                    "(run_id,run_type,status,as_of,policy_id,idempotency_key,"
                    "input_hash,owner_subject,version,created_at,updated_at) "
                    "values (:run_id,'capacity_foreign','pending',clock_timestamp(),"
                    "'foreign','capacity:foreign',:input_hash,'system:foreign',1,"
                    "clock_timestamp(),clock_timestamp())"
                ),
                {"run_id": uuid4(), "input_hash": "a" * 64},
            )
        with pytest.raises(ProbeError, match="capacity evidence graph is not exact"):
            probe.verify_evidence_scope()
        engine.dispose()

    assert counts == (sum(expected_counts.values()), 1)
