"""Consume one fenced outbox lease and deliver its rendered report artifact."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timedelta

from pydantic import ValidationError

from stonks_agent.domain.delivery import (
    DeliveryChannel,
    DeliveryCommand,
    DeliveryProcessReceipt,
    DeliveryRequest,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.outbox import OutboxLease
from stonks_agent.ports.artifact_store import ArtifactReaderPort
from stonks_agent.ports.delivery import DeliveryPort
from stonks_agent.ports.outbox import OutboxPort

_CHANNEL_LIMITS = {
    DeliveryChannel.CONSOLE: 16_384,
    DeliveryChannel.FILE: 65_536,
    DeliveryChannel.EMAIL: 131_072,
    DeliveryChannel.WEBHOOK: 16_384,
}


def deliver_outbox_lease(
    lease: OutboxLease,
    *,
    now: datetime,
    worker_id: str,
    artifacts: ArtifactReaderPort,
    channels: Mapping[DeliveryChannel, DeliveryPort],
    outbox: OutboxPort,
) -> Result[DeliveryProcessReceipt]:
    parsed = _parse_lease(lease)
    if isinstance(parsed, Failure):
        return parsed
    request = parsed.value
    channel = channels.get(request.channel)
    if channel is None:
        return _nack(lease, outbox, worker_id, now, ErrorCode.CONFIGURATION_INVALID)
    content = artifacts.read(request.content_hash)
    if isinstance(content, Failure):
        return _nack(lease, outbox, worker_id, now, content.error.code)
    if hashlib.sha256(content.value).hexdigest() != request.content_hash:
        return _nack(lease, outbox, worker_id, now, ErrorCode.CONFLICT)
    try:
        text = content.value.decode("utf-8")
    except UnicodeDecodeError:
        return _nack(lease, outbox, worker_id, now, ErrorCode.INVALID_INPUT)
    chunks = _chunk_utf8(text, _CHANNEL_LIMITS[request.channel])
    if isinstance(chunks, Failure):
        return _nack(lease, outbox, worker_id, now, chunks.error.code)
    delivered = channel.deliver(
        DeliveryCommand(
            request=request,
            media_type=_media_type(request.format),
            chunks=chunks.value,
        )
    )
    if isinstance(delivered, Failure):
        return _nack(lease, outbox, worker_id, now, delivered.error.code)
    acknowledged = outbox.ack(
        lease.outbox_id,
        worker_id=worker_id,
        lease_generation=lease.lease_generation,
        lease_nonce=lease.lease_nonce,
        now=now,
    )
    if isinstance(acknowledged, Failure):
        return acknowledged
    return Success(
        DeliveryProcessReceipt(delivery=delivered.value, outbox_ack=acknowledged.value)
    )


def _parse_lease(lease: OutboxLease) -> Result[DeliveryRequest]:
    if lease.topic != "report.delivery.requested":
        return _failure(ErrorCode.CAPABILITY_DENIED, "Outbox topic is not deliverable")
    try:
        request = DeliveryRequest.model_validate(lease.payload)
    except ValidationError:
        return _failure(ErrorCode.INVALID_INPUT, "Delivery outbox payload is invalid")
    if request.idempotency_key != lease.idempotency_key:
        return _failure(ErrorCode.CONFLICT, "Delivery idempotency identity changed")
    return Success(request)


def _chunk_utf8(value: str, maximum: int) -> Result[tuple[str, ...]]:
    if not value:
        return _failure(ErrorCode.INVALID_INPUT, "Delivery artifact is empty")
    chunks: list[str] = []
    current = ""
    current_bytes = 0
    for character in value:
        encoded = len(character.encode("utf-8"))
        if encoded > maximum:
            return _failure(
                ErrorCode.PAYLOAD_TOO_LARGE, "Delivery character is too large"
            )
        if current and current_bytes + encoded > maximum:
            chunks.append(current)
            current, current_bytes = "", 0
        current += character
        current_bytes += encoded
    if current:
        chunks.append(current)
    return Success(tuple(chunks))


def _media_type(format_name: str) -> str:
    return "text/html" if format_name == "email_html" else "text/markdown"


def _nack(
    lease: OutboxLease,
    outbox: OutboxPort,
    worker_id: str,
    now: datetime,
    code: ErrorCode,
) -> Failure:
    retry_at = now + timedelta(seconds=min(300, 2 ** min(lease.attempts, 8)))
    nacked = outbox.nack(
        lease.outbox_id,
        worker_id=worker_id,
        lease_generation=lease.lease_generation,
        lease_nonce=lease.lease_nonce,
        now=now,
        retry_at=retry_at,
        error_code=code.value,
    )
    if isinstance(nacked, Failure):
        return nacked
    return _failure(code, "Report delivery failed")


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
