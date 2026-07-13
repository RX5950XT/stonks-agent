from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from threading import RLock

from stonks_agent.domain.delivery import (
    DeliveryChannel,
    DeliveryCommand,
    DeliveryReceipt,
    DeliveryStatus,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)


class IdempotentDelivery:
    def __init__(
        self, *, channel: DeliveryChannel, clock: Callable[[], datetime]
    ) -> None:
        self._channel = channel
        self._clock = clock
        self._receipts: dict[str, DeliveryReceipt] = {}
        self._lock = RLock()

    def cached(self, command: DeliveryCommand) -> Result[DeliveryReceipt] | None:
        with self._lock:
            receipt = self._receipts.get(command.request.idempotency_key)
        if receipt is None:
            return None
        if (
            receipt.content_hash != command.request.content_hash
            or receipt.delivery_id != command.request.delivery_id
        ):
            return failure(ErrorCode.CONFLICT, "Delivery idempotency payload changed")
        return Success(receipt)

    def receipt(
        self,
        command: DeliveryCommand,
        *,
        status: DeliveryStatus,
        provider_receipt_id: str | None = None,
        reason: str | None = None,
    ) -> Success[DeliveryReceipt]:
        value = DeliveryReceipt(
            delivery_id=command.request.delivery_id,
            report_id=command.request.report_id,
            channel=self._channel,
            status=status,
            content_hash=command.request.content_hash,
            idempotency_key=command.request.idempotency_key,
            chunk_count=len(command.chunks) if status is DeliveryStatus.SENT else 0,
            provider_receipt_id=provider_receipt_id,
            delivered_at=self._clock(),
            reason=reason,
        )
        with self._lock:
            self._receipts[command.request.idempotency_key] = value
        return Success(value)


def validate_channel(
    command: DeliveryCommand, expected: DeliveryChannel
) -> Failure | None:
    if command.request.channel is not expected:
        return failure(ErrorCode.CAPABILITY_DENIED, "Delivery channel mismatch")
    actual_hash = hashlib.sha256("".join(command.chunks).encode()).hexdigest()
    if actual_hash != command.request.content_hash:
        return failure(ErrorCode.CONFLICT, "Delivery content hash changed")
    return None


def failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
