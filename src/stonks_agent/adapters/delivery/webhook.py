from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from time import sleep
from typing import Self

import httpx

from stonks_agent.adapters.delivery._common import (
    IdempotentDelivery,
    failure,
    validate_channel,
)
from stonks_agent.adapters.security.ssrf import (
    EndpointDenied,
    ExactEndpoint,
    HostResolver,
    OutboundEndpointGuard,
    PinnedHTTPTransport,
    RuntimeEnvironment,
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
        url: str | None,
        clock: Callable[[], datetime],
        client: httpx.Client | None = None,
        resolver: HostResolver | None = None,
        environment: RuntimeEnvironment = "production",
        max_retries: int = 2,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        guard = _webhook_guard(url, resolver, environment)
        if client is not None and environment != "test":
            raise ValueError("custom webhook client is test-only")
        if not 0 <= max_retries <= 5:
            raise ValueError("webhook retries are invalid")
        super().__init__(channel=DeliveryChannel.WEBHOOK, clock=clock)
        selected_client = client
        if selected_client is None and guard is not None:
            selected_client = httpx.Client(
                transport=PinnedHTTPTransport(guard),
                follow_redirects=False,
            )
        self._client, self._guard, self._url, self._max_retries, self._sleep = (
            selected_client,
            guard,
            url,
            max_retries,
            sleeper,
        )
        self._owns_client = client is None and selected_client is not None

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        del args
        self.close()

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
        assert self._client is not None
        assert self._guard is not None
        for attempt in range(self._max_retries + 1):
            try:
                self._guard.authorize(self._url)
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
                    self._guard.authorize_response(
                        status_code=response.status_code,
                        location=response.headers.get("location"),
                    )
                    if 200 <= response.status_code < 300:
                        return None
                    transient = response.status_code in {408, 429, 500, 502, 503, 504}
            except EndpointDenied:
                return failure(ErrorCode.EGRESS_DENIED, "Webhook endpoint is denied")
            except httpx.HTTPError:
                transient = True
            if not transient or attempt >= self._max_retries:
                return failure(ErrorCode.DATA_UNAVAILABLE, "Webhook delivery failed")
            self._sleep(0.25 * (2**attempt))
        return failure(ErrorCode.DATA_UNAVAILABLE, "Webhook delivery failed")


def _webhook_guard(
    url: str | None,
    resolver: HostResolver | None,
    environment: RuntimeEnvironment,
) -> OutboundEndpointGuard | None:
    if url is None:
        return None
    try:
        endpoint = ExactEndpoint.from_url(url, environment=environment)
    except ValueError as error:
        raise ValueError("webhook URL is invalid") from error
    if endpoint.scheme != "https":
        raise ValueError("webhook URL is invalid")
    return OutboundEndpointGuard(endpoint, resolver=resolver)
