from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from stonks_agent.adapters.delivery._common import (
    IdempotentDelivery,
    failure,
    validate_channel,
)
from stonks_agent.domain.delivery import (
    DeliveryChannel,
    DeliveryCommand,
    DeliveryReceipt,
    DeliveryStatus,
)
from stonks_agent.domain.errors import ErrorCode, Result


class EmailSender(Protocol):
    def send(
        self, *, recipient: str, subject: str, html: str, idempotency_key: str
    ) -> str: ...


class EmailDeliveryAdapter(IdempotentDelivery):
    def __init__(
        self,
        *,
        sender: EmailSender,
        recipient: str | None,
        clock: Callable[[], datetime],
    ) -> None:
        super().__init__(channel=DeliveryChannel.EMAIL, clock=clock)
        self._sender = sender
        self._recipient = recipient

    def deliver(self, command: DeliveryCommand) -> Result[DeliveryReceipt]:
        denied = validate_channel(command, DeliveryChannel.EMAIL)
        if denied is not None:
            return denied
        cached = self.cached(command)
        if cached is not None:
            return cached
        if self._recipient is None:
            return self.receipt(
                command,
                status=DeliveryStatus.SKIPPED,
                reason="email_not_configured",
            )
        if command.media_type != "text/html":
            return failure(ErrorCode.INVALID_INPUT, "Email delivery requires HTML")
        try:
            receipt_id = self._sender.send(
                recipient=self._recipient,
                subject=f"Report {command.request.report_id}",
                html="".join(command.chunks),
                idempotency_key=command.request.idempotency_key,
            )
        except Exception:
            return failure(ErrorCode.DATA_UNAVAILABLE, "Email delivery failed")
        return self.receipt(
            command,
            status=DeliveryStatus.SENT,
            provider_receipt_id=receipt_id,
        )
