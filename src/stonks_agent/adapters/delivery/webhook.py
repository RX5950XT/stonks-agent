from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from time import sleep
from urllib.parse import urlsplit

import httpx

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


class WebhookDeliveryAdapter(IdempotentDelivery):
    def __init__(
        self,
        *,
        client: httpx.Client,
        url: str | None,
        clock: Callable[[], datetime],
        max_retries: int = 2,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        if url is not None:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("webhook URL is invalid")
        if not 0 <= max_retries <= 5:
            raise ValueError("webhook retries are invalid")
        super().__init__(channel=DeliveryChannel.WEBHOOK, clock=clock)
        self._client, self._url, self._max_retries, self._sleep = (
            client,
            url,
            max_retries,
            sleeper,
        )

    def deliver(self, command: DeliveryCommand) -> Result[DeliveryReceipt]:
        denied = validate_channel(command, DeliveryChannel.WEBHOOK)
        if denied is not None:
            return denied
        cached = self.cached(command)
        if cached is not None:
            return cached
        if self._url is None:
            return self.receipt(
                command,
                status=DeliveryStatus.SKIPPED,
                reason="webhook_not_configured",
            )
        for index, chunk in enumerate(command.chunks):
            sent = self._send_chunk(command, index, chunk)
            if sent is not None:
                return sent
        return self.receipt(command, status=DeliveryStatus.SENT)

    def _send_chunk(
        self, command: DeliveryCommand, index: int, chunk: str
    ) -> Result[DeliveryReceipt] | None:
        assert self._url is not None
        for attempt in range(self._max_retries + 1):
            try:
                with self._client.stream(
                    "POST",
                    self._url,
                    content=chunk.encode("utf-8"),
                    headers={
                        "Content-Type": command.media_type,
                        "Accept-Encoding": "identity",
                        "Idempotency-Key": f"{command.request.idempotency_key}:{index}",
                    },
                    timeout=httpx.Timeout(10),
                    follow_redirects=False,
                ) as response:
                    if 200 <= response.status_code < 300:
                        return None
                    transient = response.status_code in {408, 429, 500, 502, 503, 504}
            except httpx.HTTPError:
                transient = True
            if not transient or attempt >= self._max_retries:
                return failure(ErrorCode.DATA_UNAVAILABLE, "Webhook delivery failed")
            self._sleep(0.25 * (2**attempt))
        return failure(ErrorCode.DATA_UNAVAILABLE, "Webhook delivery failed")
