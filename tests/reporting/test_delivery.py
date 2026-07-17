from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from support.telemetry import RecordingOperationRecorder

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.adapters.delivery.console import ConsoleDeliveryAdapter
from stonks_agent.adapters.delivery.email import EmailDeliveryAdapter
from stonks_agent.adapters.delivery.file import FileDeliveryAdapter
from stonks_agent.adapters.delivery.webhook import WebhookDeliveryAdapter
from stonks_agent.application.reporting.deliver import _chunk_utf8, deliver_outbox_lease
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.delivery import (
    DeliveryChannel,
    DeliveryCommand,
    DeliveryRequest,
    DeliveryStatus,
)
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError, Success
from stonks_agent.domain.outbox import OutboxAckReceipt, OutboxLease
from stonks_agent.domain.telemetry import ComponentName, OperationName
from stonks_contracts.evidence import Sensitivity

NOW = datetime(2026, 7, 13, 7, tzinfo=UTC)
OUTBOX_ID = UUID("35000000-0000-4000-8000-000000000001")
REPORT_ID = UUID("35000000-0000-4000-8000-000000000002")
DELIVERY_ID = UUID("35000000-0000-4000-8000-000000000003")
NONCE = UUID("35000000-0000-4000-8000-000000000004")
CONTENT = "研究報告\n" + "x" * 20_000


class PublicResolver:
    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        assert host == "hooks.example"
        assert port == 443
        return ("93.184.216.34",)


def request(
    channel: DeliveryChannel = DeliveryChannel.CONSOLE, *, content: str = CONTENT
) -> DeliveryRequest:
    return DeliveryRequest(
        delivery_id=DELIVERY_ID,
        report_id=REPORT_ID,
        channel=channel,
        format="markdown_full"
        if channel is not DeliveryChannel.EMAIL
        else "email_html",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        idempotency_key=f"report:{REPORT_ID}:{channel.value}",
        required=channel in {DeliveryChannel.CONSOLE, DeliveryChannel.FILE},
    )


def lease(
    channel: DeliveryChannel = DeliveryChannel.CONSOLE, **overrides: object
) -> OutboxLease:
    delivery = request(channel)
    values: dict[str, object] = {
        "outbox_id": OUTBOX_ID,
        "aggregate_type": "report",
        "aggregate_id": str(REPORT_ID),
        "sequence": 1,
        "topic": "report.delivery.requested",
        "payload": delivery.model_dump(mode="json"),
        "idempotency_key": delivery.idempotency_key,
        "lease_owner": "delivery-worker",
        "lease_until": NOW + timedelta(minutes=1),
        "lease_generation": 2,
        "lease_nonce": NONCE,
        "attempts": 1,
    }
    values.update(overrides)
    return OutboxLease.model_validate(values)


def artifacts(content: str = CONTENT) -> MemoryArtifactStore:
    store = MemoryArtifactStore()
    stored = store.finalize(
        content.encode(),
        metadata=ArtifactMetadata(
            media_type="text/markdown",
            license_tag="Apache-2.0",
            sensitivity=Sensitivity.INTERNAL,
            source="test",
        ),
        finalized_at=NOW,
    )
    assert isinstance(stored, Success)
    return store


class Outbox:
    def __init__(self, *, ack_failure: Failure | None = None) -> None:
        self.acks: list[UUID] = []
        self.nacks: list[tuple[UUID, str]] = []
        self.ack_failure = ack_failure

    def ack(self, outbox_id: UUID, **kwargs: object) -> object:
        self.acks.append(outbox_id)
        if self.ack_failure:
            return self.ack_failure
        return Success(
            OutboxAckReceipt(
                outbox_id=outbox_id,
                worker_id=str(kwargs["worker_id"]),
                lease_generation=int(kwargs["lease_generation"]),
                lease_nonce=kwargs["lease_nonce"],
                published_at=NOW,
            )
        )

    def nack(self, outbox_id: UUID, **kwargs: object) -> object:
        self.nacks.append((outbox_id, str(kwargs["error_code"])))
        return Success(True)


def test_outbox_delivery_chunks_artifact_then_acks_exact_fence() -> None:
    written: list[str] = []
    channel = ConsoleDeliveryAdapter(writer=written.append, clock=lambda: NOW)
    outbox = Outbox()
    telemetry = RecordingOperationRecorder()

    result = deliver_outbox_lease(
        lease(),
        now=NOW,
        worker_id="delivery-worker",
        artifacts=artifacts(),
        channels={DeliveryChannel.CONSOLE: channel},
        outbox=outbox,  # type: ignore[arg-type]
        telemetry=telemetry,
    )

    assert isinstance(result, Success)
    assert "".join(written) == CONTENT
    assert result.value.delivery.chunk_count == 2
    assert result.value.delivery.status is DeliveryStatus.SENT
    assert outbox.acks == [OUTBOX_ID]
    assert outbox.nacks == []
    assert telemetry.calls == [(ComponentName.DELIVERY, OperationName.DELIVER)]


def test_failure_nacks_with_safe_code_and_never_acks() -> None:
    outbox = Outbox()

    result = deliver_outbox_lease(
        lease(),
        now=NOW,
        worker_id="delivery-worker",
        artifacts=MemoryArtifactStore(),
        channels={
            DeliveryChannel.CONSOLE: ConsoleDeliveryAdapter(
                writer=lambda _: None, clock=lambda: NOW
            )
        },
        outbox=outbox,  # type: ignore[arg-type]
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.NOT_FOUND
    assert outbox.acks == []
    assert outbox.nacks == [(OUTBOX_ID, "not_found")]
    assert "artifact://" not in result.error.message


def test_console_and_file_are_idempotent_and_file_stays_in_fixed_root(
    tmp_path: Path,
) -> None:
    command = DeliveryCommand(
        request=request(DeliveryChannel.FILE, content="hello world"),
        media_type="text/markdown",
        chunks=("hello", " world"),
    )
    file_adapter = FileDeliveryAdapter(output_directory=tmp_path, clock=lambda: NOW)

    first = file_adapter.deliver(command)
    second = file_adapter.deliver(command)

    assert isinstance(first, Success)
    assert second == first
    delivered = tmp_path / str(first.value.provider_receipt_id)
    assert delivered.parent == tmp_path
    assert delivered.read_text("utf-8") == "hello world"


def test_adapter_rejects_content_hash_mismatch_before_side_effect(
    tmp_path: Path,
) -> None:
    command = DeliveryCommand(
        request=request(DeliveryChannel.FILE, content="expected"),
        media_type="text/markdown",
        chunks=("tampered",),
    )
    adapter = FileDeliveryAdapter(output_directory=tmp_path, clock=lambda: NOW)

    result = adapter.deliver(command)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert list(tmp_path.iterdir()) == []


def test_file_delivery_never_overwrites_different_existing_artifact(
    tmp_path: Path,
) -> None:
    first = DeliveryCommand(
        request=request(DeliveryChannel.FILE, content="first"),
        media_type="text/markdown",
        chunks=("first",),
    )
    second = DeliveryCommand(
        request=request(DeliveryChannel.FILE, content="second"),
        media_type="text/markdown",
        chunks=("second",),
    )
    assert isinstance(
        FileDeliveryAdapter(output_directory=tmp_path, clock=lambda: NOW).deliver(
            first
        ),
        Success,
    )

    result = FileDeliveryAdapter(output_directory=tmp_path, clock=lambda: NOW).deliver(
        second
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert next(tmp_path.iterdir()).read_text("utf-8") == "first"


def test_unconfigured_email_and_webhook_return_skipped_receipts() -> None:
    class Sender:
        def send(self, **kwargs: object) -> str:
            raise AssertionError(kwargs)

    email = EmailDeliveryAdapter(sender=Sender(), recipient=None, clock=lambda: NOW)
    email_result = email.deliver(
        DeliveryCommand(
            request=request(DeliveryChannel.EMAIL, content="<p>report</p>"),
            media_type="text/html",
            chunks=("<p>report</p>",),
        )
    )
    webhook = WebhookDeliveryAdapter(
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(500))
        ),
        url=None,
        clock=lambda: NOW,
        environment="test",
    )
    webhook_result = webhook.deliver(
        DeliveryCommand(
            request=request(DeliveryChannel.WEBHOOK, content="report"),
            media_type="text/markdown",
            chunks=("report",),
        )
    )

    assert isinstance(email_result, Success)
    assert email_result.value.status is DeliveryStatus.SKIPPED
    assert isinstance(webhook_result, Success)
    assert webhook_result.value.status is DeliveryStatus.SKIPPED


def test_console_and_email_failures_are_structured_and_email_can_send() -> None:
    def broken_writer(_: str) -> None:
        raise OSError("secret path must not escape")

    console_result = ConsoleDeliveryAdapter(
        writer=broken_writer, clock=lambda: NOW
    ).deliver(
        DeliveryCommand(
            request=request(DeliveryChannel.CONSOLE, content="report"),
            media_type="text/markdown",
            chunks=("report",),
        )
    )

    class Sender:
        def __init__(self) -> None:
            self.calls = 0

        def send(self, **kwargs: object) -> str:
            self.calls += 1
            assert (
                kwargs["idempotency_key"]
                == request(
                    DeliveryChannel.EMAIL, content="<p>report</p>"
                ).idempotency_key
            )
            return "provider-1"

    sender = Sender()
    email = EmailDeliveryAdapter(
        sender=sender, recipient="research@example.test", clock=lambda: NOW
    )
    email_result = email.deliver(
        DeliveryCommand(
            request=request(DeliveryChannel.EMAIL, content="<p>report</p>"),
            media_type="text/html",
            chunks=("<p>report</p>",),
        )
    )
    invalid_media = EmailDeliveryAdapter(
        sender=sender, recipient="research@example.test", clock=lambda: NOW
    ).deliver(
        DeliveryCommand(
            request=request(DeliveryChannel.EMAIL, content="report"),
            media_type="text/markdown",
            chunks=("report",),
        )
    )

    assert isinstance(console_result, Failure)
    assert console_result.error.code is ErrorCode.INTERNAL_ERROR
    assert "secret" not in console_result.error.message
    assert isinstance(email_result, Success)
    assert email_result.value.provider_receipt_id == "provider-1"
    assert sender.calls == 1
    assert isinstance(invalid_media, Failure)
    assert invalid_media.error.code is ErrorCode.INVALID_INPUT


def test_webhook_uses_fixed_https_no_redirect_idempotency_and_bounded_retry() -> None:
    calls: list[httpx.Request] = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        calls.append(incoming)
        status = 503 if len(calls) == 1 else 204
        return httpx.Response(status, request=incoming)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    adapter = WebhookDeliveryAdapter(
        client=client,
        url="https://hooks.example/reports",
        clock=lambda: NOW,
        resolver=PublicResolver(),
        environment="test",
        max_retries=1,
        sleeper=lambda _: None,
    )
    command = DeliveryCommand(
        request=request(DeliveryChannel.WEBHOOK, content="report"),
        media_type="text/markdown",
        chunks=("report",),
    )

    with client:
        result = adapter.deliver(command)

    assert isinstance(result, Success)
    assert len(calls) == 2
    assert all(item.url == "https://hooks.example/reports" for item in calls)
    assert all(item.headers["Idempotency-Key"].endswith(":0") for item in calls)
    assert all(item.headers["Accept-Encoding"] == "identity" for item in calls)


def test_webhook_rejects_unsafe_url_and_does_not_retry_permanent_error() -> None:
    for unsafe in (
        "http://hooks.example/reports",
        "https://user@hooks.example/reports",
        "https://hooks.example/reports?token=secret",
    ):
        try:
            WebhookDeliveryAdapter(
                client=httpx.Client(),
                url=unsafe,
                clock=lambda: NOW,
                resolver=PublicResolver(),
                environment="test",
            )
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe webhook URL accepted")

    calls = 0

    def handler(incoming: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, request=incoming)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = WebhookDeliveryAdapter(
            client=client,
            url="https://hooks.example/reports",
            clock=lambda: NOW,
            resolver=PublicResolver(),
            environment="test",
            max_retries=2,
            sleeper=lambda _: None,
        ).deliver(
            DeliveryCommand(
                request=request(DeliveryChannel.WEBHOOK, content="report"),
                media_type="text/markdown",
                chunks=("report",),
            )
        )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.DATA_UNAVAILABLE
    assert calls == 1


def test_webhook_denies_private_dns_and_redirect_before_retry() -> None:
    class PrivateResolver:
        def resolve(self, host: str, port: int) -> tuple[str, ...]:
            assert (host, port) == ("hooks.example", 443)
            return ("169.254.169.254",)

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            307,
            headers={"location": "http://169.254.169.254/latest/meta-data"},
            request=request,
        )

    command = DeliveryCommand(
        request=request(DeliveryChannel.WEBHOOK, content="report"),
        media_type="text/markdown",
        chunks=("report",),
    )
    private = WebhookDeliveryAdapter(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        url="https://hooks.example/reports",
        clock=lambda: NOW,
        resolver=PrivateResolver(),
        environment="test",
        max_retries=2,
    ).deliver(command)
    redirected = WebhookDeliveryAdapter(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        url="https://hooks.example/reports",
        clock=lambda: NOW,
        resolver=PublicResolver(),
        environment="test",
        max_retries=2,
    ).deliver(command)

    assert isinstance(private, Failure)
    assert private.error.code is ErrorCode.EGRESS_DENIED
    assert isinstance(redirected, Failure)
    assert redirected.error.code is ErrorCode.EGRESS_DENIED
    assert calls == 1


def test_webhook_rejects_custom_http_client_outside_tests() -> None:
    with pytest.raises(ValueError, match="test-only"):
        WebhookDeliveryAdapter(
            client=httpx.Client(),
            url="https://hooks.example/reports",
            clock=lambda: NOW,
            resolver=PublicResolver(),
        )


def test_delivery_identity_conflict_and_ack_failure_fail_closed() -> None:
    conflict = deliver_outbox_lease(
        lease(idempotency_key="changed"),
        now=NOW,
        worker_id="delivery-worker",
        artifacts=artifacts(),
        channels={},
        outbox=Outbox(),  # type: ignore[arg-type]
    )
    ack_failure = Failure(
        StructuredError(code=ErrorCode.CONFLICT, message="Lease expired")
    )
    outbox = Outbox(ack_failure=ack_failure)
    result = deliver_outbox_lease(
        lease(),
        now=NOW,
        worker_id="delivery-worker",
        artifacts=artifacts(),
        channels={
            DeliveryChannel.CONSOLE: ConsoleDeliveryAdapter(
                writer=lambda _: None, clock=lambda: NOW
            )
        },
        outbox=outbox,  # type: ignore[arg-type]
    )

    assert isinstance(conflict, Failure)
    assert conflict.error.code is ErrorCode.CONFLICT
    assert result == ack_failure
    assert outbox.acks == [OUTBOX_ID]


def test_utf8_chunking_respects_bytes_and_invalid_topic_is_denied() -> None:
    chunked = _chunk_utf8("台灣abc", 4)
    assert isinstance(chunked, Success)
    assert "".join(chunked.value) == "台灣abc"
    assert all(len(item.encode()) <= 4 for item in chunked.value)

    outbox = Outbox()
    denied = deliver_outbox_lease(
        lease(topic="other.topic"),
        now=NOW,
        worker_id="delivery-worker",
        artifacts=artifacts(),
        channels={},
        outbox=outbox,  # type: ignore[arg-type]
    )
    assert isinstance(denied, Failure)
    assert denied.error.code is ErrorCode.CAPABILITY_DENIED
    assert outbox.acks == outbox.nacks == []
