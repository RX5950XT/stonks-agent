from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stonks_agent.adapters.fakes.jobs import InMemoryJobRunner
from stonks_agent.application.workflows.run_cycle import (
    IdempotencyConflict,
    LateResultRejected,
)

NOW = datetime(2026, 1, 2, 21, 0, tzinfo=UTC)


def test_core_runner_commits_result_event_and_outbox_once() -> None:
    runner = InMemoryJobRunner(clock=NOW)
    job_id = runner.enqueue("research", {"snapshot_id": "snapshot-1"}, "job-key")
    attempt = runner.claim(job_id, lease_for=timedelta(seconds=30))

    receipt = runner.accept_result(
        job_id=job_id,
        generation=attempt.generation,
        nonce=attempt.nonce,
        result={"artifact_hash": "a" * 64},
    )
    duplicate = runner.accept_result(
        job_id=job_id,
        generation=attempt.generation,
        nonce=attempt.nonce,
        result={"artifact_hash": "a" * 64},
    )

    assert duplicate == receipt
    assert runner.domain_events == (receipt.event,)
    assert runner.outbox == (receipt.outbox_message,)


def test_expired_attempt_cannot_commit_late_result() -> None:
    runner = InMemoryJobRunner(clock=NOW)
    job_id = runner.enqueue("forecast", {"snapshot_id": "snapshot-1"}, "job-key")
    stale = runner.claim(job_id, lease_for=timedelta(seconds=1))
    runner.advance(timedelta(seconds=2))
    current = runner.reclaim(job_id, lease_for=timedelta(seconds=30))

    with pytest.raises(LateResultRejected):
        runner.accept_result(
            job_id=job_id,
            generation=stale.generation,
            nonce=stale.nonce,
            result={"artifact_hash": "b" * 64},
        )

    assert runner.domain_events == ()
    assert runner.outbox == ()
    assert runner.quarantined_results[0].reason == "stale_attempt"
    runner.accept_result(
        job_id=job_id,
        generation=current.generation,
        nonce=current.nonce,
        result={"artifact_hash": "c" * 64},
    )
    assert len(runner.domain_events) == 1


def test_job_idempotency_key_rejects_different_payload() -> None:
    runner = InMemoryJobRunner(clock=NOW)
    runner.enqueue("research", {"snapshot_id": "snapshot-1"}, "job-key")

    with pytest.raises(IdempotencyConflict):
        runner.enqueue("research", {"snapshot_id": "snapshot-2"}, "job-key")
