from __future__ import annotations

from datetime import timedelta

import pytest

from stonks_agent.adapters.fakes.jobs import InMemoryJobRunner
from stonks_agent.application.workflows.run_cycle import (
    IdempotencyConflict,
    LateResultRejected,
)

from .helpers import NOW


def test_duplicate_result_has_exactly_one_event_and_outbox_side_effect() -> None:
    runner = InMemoryJobRunner(clock=NOW)
    job_id = runner.enqueue("research", {"snapshot_id": "snapshot-1"}, "fault-job")
    attempt = runner.claim(job_id, lease_for=timedelta(seconds=30))
    result = {"artifact_hash": "b" * 64}

    receipt = runner.accept_result(
        job_id=job_id,
        generation=attempt.generation,
        nonce=attempt.nonce,
        result=result,
    )
    duplicate = runner.accept_result(
        job_id=job_id,
        generation=attempt.generation,
        nonce=attempt.nonce,
        result=result,
    )

    assert duplicate == receipt
    assert runner.domain_events == (receipt.event,)
    assert runner.outbox == (receipt.outbox_message,)
    with pytest.raises(IdempotencyConflict):
        runner.accept_result(
            job_id=job_id,
            generation=attempt.generation,
            nonce=attempt.nonce,
            result={"artifact_hash": "c" * 64},
        )
    assert runner.domain_events == (receipt.event,)
    assert runner.outbox == (receipt.outbox_message,)


def test_expired_result_is_quarantined_and_current_lease_recovers_once() -> None:
    runner = InMemoryJobRunner(clock=NOW)
    job_id = runner.enqueue("forecast", {"snapshot_id": "snapshot-1"}, "lease-job")
    expired = runner.claim(job_id, lease_for=timedelta(seconds=1))
    runner.advance(timedelta(seconds=2))
    current = runner.reclaim(job_id, lease_for=timedelta(seconds=30))

    with pytest.raises(LateResultRejected):
        runner.accept_result(
            job_id=job_id,
            generation=expired.generation,
            nonce=expired.nonce,
            result={"artifact_hash": "d" * 64},
        )

    assert runner.domain_events == ()
    assert runner.outbox == ()
    assert len(runner.quarantined_results) == 1
    accepted = runner.accept_result(
        job_id=job_id,
        generation=current.generation,
        nonce=current.nonce,
        result={"artifact_hash": "e" * 64},
    )
    assert runner.domain_events == (accepted.event,)
    assert runner.outbox == (accepted.outbox_message,)
