"""Report channel delivery boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.delivery import DeliveryCommand, DeliveryReceipt
from stonks_agent.domain.errors import Result


@runtime_checkable
class DeliveryPort(Protocol):
    def deliver(self, command: DeliveryCommand) -> Result[DeliveryReceipt]: ...
