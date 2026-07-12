"""In-memory durable-job semantics with generation and nonce fencing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
from typing import Any

from stonks_agent.application.workflows.run_cycle import (
    IdempotencyConflict,
    LateResultRejected,
    stable_hash,
)


@dataclass(frozen=True, slots=True)
class JobAttempt:
    job_id: str
    generation: int
    nonce: str
    lease_until: datetime


@dataclass(frozen=True, slots=True)
class JobDomainEvent:
    event_id: str
    job_id: str
    event_type: str
    payload_hash: str


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    message_id: str
    topic: str
    payload_hash: str


@dataclass(frozen=True, slots=True)
class JobResultReceipt:
    receipt_id: str
    result_hash: str
    event: JobDomainEvent
    outbox_message: OutboxMessage


@dataclass(frozen=True, slots=True)
class QuarantinedResult:
    job_id: str
    generation: int
    reason: str
    result_hash: str


@dataclass(slots=True)
class _Job:
    job_id: str
    job_type: str
    payload_hash: str
    idempotency_key: str
    generation: int = 0
    nonce: str | None = None
    lease_until: datetime | None = None
    receipt: JobResultReceipt | None = None


class InMemoryJobRunner:
    """Model the core-owned transaction that commits worker results."""

    def __init__(self, *, clock: datetime) -> None:
        if clock.tzinfo is None:
            raise ValueError("clock must be timezone-aware")
        self._clock = clock
        self._lock = RLock()
        self._jobs: dict[str, _Job] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._events: list[JobDomainEvent] = []
        self._outbox: list[OutboxMessage] = []
        self._quarantined: list[QuarantinedResult] = []

    @property
    def domain_events(self) -> tuple[JobDomainEvent, ...]:
        with self._lock:
            return tuple(self._events)

    @property
    def outbox(self) -> tuple[OutboxMessage, ...]:
        with self._lock:
            return tuple(self._outbox)

    @property
    def quarantined_results(self) -> tuple[QuarantinedResult, ...]:
        with self._lock:
            return tuple(self._quarantined)

    def advance(self, duration: timedelta) -> None:
        if duration < timedelta(0):
            raise ValueError("cannot move clock backwards")
        with self._lock:
            self._clock += duration

    def enqueue(self, job_type: str, payload: dict[str, Any], key: str) -> str:
        payload_hash = stable_hash((job_type, payload))
        with self._lock:
            cached = self._idempotency.get(key)
            if cached is not None:
                cached_hash, job_id = cached
                if cached_hash != payload_hash:
                    raise IdempotencyConflict("job idempotency payload mismatch")
                return job_id
            job_id = f"job_{stable_hash((key, payload_hash))[:24]}"
            self._jobs[job_id] = _Job(
                job_id=job_id,
                job_type=job_type,
                payload_hash=payload_hash,
                idempotency_key=key,
            )
            self._idempotency[key] = (payload_hash, job_id)
            return job_id

    def claim(self, job_id: str, *, lease_for: timedelta) -> JobAttempt:
        if lease_for <= timedelta(0):
            raise ValueError("lease_for must be positive")
        with self._lock:
            job = self._jobs[job_id]
            if job.receipt is not None:
                raise RuntimeError("completed job cannot be claimed")
            if job.lease_until is not None and job.lease_until > self._clock:
                raise RuntimeError("job already has an active lease")
            return self._new_attempt(job, lease_for)

    def reclaim(self, job_id: str, *, lease_for: timedelta) -> JobAttempt:
        with self._lock:
            job = self._jobs[job_id]
            if job.lease_until is None or job.lease_until > self._clock:
                raise RuntimeError("job lease has not expired")
            return self._new_attempt(job, lease_for)

    def accept_result(
        self,
        *,
        job_id: str,
        generation: int,
        nonce: str,
        result: dict[str, Any],
    ) -> JobResultReceipt:
        result_hash = stable_hash(result)
        with self._lock:
            job = self._jobs[job_id]
            if job.receipt is not None:
                return self._existing_receipt(job, generation, nonce, result_hash)
            if not self._owns_active_attempt(job, generation, nonce):
                self._quarantine(job_id, generation, result_hash)
                raise LateResultRejected("worker result does not own active attempt")
            receipt = self._build_receipt(job, result_hash)
            job.receipt = receipt
            self._events.append(receipt.event)
            self._outbox.append(receipt.outbox_message)
            return receipt

    def _new_attempt(self, job: _Job, lease_for: timedelta) -> JobAttempt:
        job.generation += 1
        job.nonce = stable_hash((job.job_id, job.generation, self._clock.isoformat()))[
            :32
        ]
        job.lease_until = self._clock + lease_for
        return JobAttempt(
            job_id=job.job_id,
            generation=job.generation,
            nonce=job.nonce,
            lease_until=job.lease_until,
        )

    def _owns_active_attempt(self, job: _Job, generation: int, nonce: str) -> bool:
        return (
            generation == job.generation
            and nonce == job.nonce
            and job.lease_until is not None
            and self._clock <= job.lease_until
        )

    def _existing_receipt(
        self, job: _Job, generation: int, nonce: str, result_hash: str
    ) -> JobResultReceipt:
        assert job.receipt is not None
        if generation != job.generation or nonce != job.nonce:
            self._quarantine(job.job_id, generation, result_hash)
            raise LateResultRejected("completed attempt does not match")
        if result_hash != job.receipt.result_hash:
            raise IdempotencyConflict("accepted result payload changed")
        return job.receipt

    def _quarantine(self, job_id: str, generation: int, result_hash: str) -> None:
        self._quarantined.append(
            QuarantinedResult(
                job_id=job_id,
                generation=generation,
                reason="stale_attempt",
                result_hash=result_hash,
            )
        )

    @staticmethod
    def _build_receipt(job: _Job, result_hash: str) -> JobResultReceipt:
        receipt_id = f"job_receipt_{stable_hash((job.job_id, result_hash))[:24]}"
        event = JobDomainEvent(
            event_id=f"event_{stable_hash((receipt_id, 'event'))[:24]}",
            job_id=job.job_id,
            event_type="worker.result_accepted",
            payload_hash=result_hash,
        )
        outbox = OutboxMessage(
            message_id=f"outbox_{stable_hash((receipt_id, 'outbox'))[:24]}",
            topic="worker.result.accepted",
            payload_hash=result_hash,
        )
        return JobResultReceipt(
            receipt_id=receipt_id,
            result_hash=result_hash,
            event=event,
            outbox_message=outbox,
        )
