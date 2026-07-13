from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

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


class ConsoleDeliveryAdapter(IdempotentDelivery):
    def __init__(
        self, *, writer: Callable[[str], None], clock: Callable[[], datetime]
    ) -> None:
        super().__init__(channel=DeliveryChannel.CONSOLE, clock=clock)
        self._writer = writer

    def deliver(self, command: DeliveryCommand) -> Result[DeliveryReceipt]:
        denied = validate_channel(command, DeliveryChannel.CONSOLE)
        if denied is not None:
            return denied
        cached = self.cached(command)
        if cached is not None:
            return cached
        try:
            for chunk in command.chunks:
                self._writer(chunk)
        except Exception:
            return failure(ErrorCode.INTERNAL_ERROR, "Console delivery failed")
        return self.receipt(command, status=DeliveryStatus.SENT)
