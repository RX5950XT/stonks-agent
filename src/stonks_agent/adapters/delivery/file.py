from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

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


class FileDeliveryAdapter(IdempotentDelivery):
    def __init__(
        self, *, output_directory: Path, clock: Callable[[], datetime]
    ) -> None:
        resolved = output_directory.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("delivery output directory is invalid")
        super().__init__(channel=DeliveryChannel.FILE, clock=clock)
        self._output = resolved

    def deliver(self, command: DeliveryCommand) -> Result[DeliveryReceipt]:
        denied = validate_channel(command, DeliveryChannel.FILE)
        if denied is not None:
            return denied
        cached = self.cached(command)
        if cached is not None:
            return cached
        suffix = ".html" if command.media_type == "text/html" else ".md"
        target = (
            self._output
            / f"{command.request.report_id}.{command.request.format}{suffix}"
        ).resolve()
        if target.parent != self._output:
            return failure(
                ErrorCode.FORBIDDEN, "Delivery path escaped output directory"
            )
        if target.exists():
            try:
                existing_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            except OSError:
                return failure(ErrorCode.INTERNAL_ERROR, "File delivery failed")
            if existing_hash != command.request.content_hash:
                return failure(
                    ErrorCode.CONFLICT, "File delivery target already differs"
                )
            return self.receipt(
                command,
                status=DeliveryStatus.SENT,
                provider_receipt_id=target.name,
            )
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        try:
            temporary.write_text(
                "".join(command.chunks), encoding="utf-8", newline="\n"
            )
            os.replace(temporary, target)
        except OSError:
            return failure(ErrorCode.INTERNAL_ERROR, "File delivery failed")
        return self.receipt(
            command,
            status=DeliveryStatus.SENT,
            provider_receipt_id=target.name,
        )
