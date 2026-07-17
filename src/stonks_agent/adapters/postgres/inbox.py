"""Transactional PostgreSQL inbox with duplicate side-effect suppression."""

from __future__ import annotations

from collections.abc import Callable

import structlog
from pydantic import ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.durable_trace import trace_carrier_from_columns
from stonks_agent.adapters.postgres.models import InboxRow
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.inbox import InboxMessage, InboxReceipt

InboxHandler = Callable[[Session], dict[str, object]]


class PostgresInbox:
    """Run a DB-only handler once for each consumer/message identity.

    The handler must perform every side effect through the supplied Session. This
    keeps the handler result, side effects, and immutable inbox receipt in one
    transaction. External side effects belong behind the durable outbox.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def consume(
        self,
        message: InboxMessage,
        handler: InboxHandler,
    ) -> Result[InboxReceipt]:
        try:
            with Session(self._engine, expire_on_commit=False) as session:
                with session.begin():
                    _lock_message(session, message)
                    existing = session.get(
                        InboxRow,
                        (message.consumer, message.message_id),
                    )
                    if existing is not None:
                        return _existing_receipt(existing, message)
                    result = handler(session)
                    receipt = InboxReceipt(
                        consumer=message.consumer,
                        message_id=message.message_id,
                        payload_hash=message.payload_hash,
                        duplicate=False,
                        processed_at=message.processed_at,
                        result=result,
                        trace_carrier=message.trace_carrier,
                        correlation_id=message.correlation_id,
                    )
                    session.add(_row(message, receipt))
                    session.flush()
                return Success(receipt)
        except (SQLAlchemyError, ValidationError, ValueError) as error:
            _log_failure(message, error)
            return _failure(ErrorCode.INTERNAL_ERROR, "Inbox processing failed")
        except Exception as error:
            _log_failure(message, error)
            return _failure(ErrorCode.INTERNAL_ERROR, "Inbox handler failed")


def _lock_message(session: Session, message: InboxMessage) -> None:
    key = f"{len(message.consumer)}:{message.consumer}{message.message_id}"
    session.execute(
        text("select pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": key},
    )


def _existing_receipt(
    row: InboxRow,
    message: InboxMessage,
) -> Result[InboxReceipt]:
    if row.payload_hash != message.payload_hash:
        return _failure(
            ErrorCode.CONFLICT,
            "Inbox message payload conflicts with prior receipt",
        )
    if row.processed_at is None or row.result is None:
        return _failure(ErrorCode.CONFLICT, "Inbox receipt is incomplete")
    try:
        receipt = InboxReceipt(
            consumer=row.consumer,
            message_id=row.message_id,
            payload_hash=row.payload_hash,
            duplicate=True,
            processed_at=row.processed_at,
            result=row.result,
            trace_carrier=trace_carrier_from_columns(
                row.traceparent,
                row.tracestate,
            ),
            correlation_id=row.correlation_id,
        )
    except ValidationError:
        return _failure(ErrorCode.CONFLICT, "Inbox receipt is invalid")
    return Success(receipt)


def _row(message: InboxMessage, receipt: InboxReceipt) -> InboxRow:
    return InboxRow(
        consumer=message.consumer,
        message_id=message.message_id,
        payload_hash=message.payload_hash,
        received_at=message.received_at,
        processed_at=receipt.processed_at,
        result=receipt.result,
        traceparent=(
            message.trace_carrier.traceparent
            if message.trace_carrier is not None
            else None
        ),
        tracestate=(
            message.trace_carrier.tracestate
            if message.trace_carrier is not None
            else None
        ),
        correlation_id=message.correlation_id,
    )


def _log_failure(message: InboxMessage, error: Exception) -> None:
    structlog.get_logger(__name__).error(
        "inbox_processing_failed",
        consumer=message.consumer,
        message_id=message.message_id,
        error_type=type(error).__name__,
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
