"""Fail-closed JSONB binding for durable payloads that must never hold secrets."""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.exc import DontWrapMixin, SQLAlchemyError
from sqlalchemy.types import TypeDecorator

from stonks_agent.domain.redaction import SecretLeakDetected, ensure_secret_free


class SecretPersistenceError(SQLAlchemyError, DontWrapMixin):
    """Public-safe rejection without SQLAlchemy attaching sensitive parameters."""


class SecretFreeJSONB(TypeDecorator[dict[str, object]]):
    """Reject secret-shaped data before SQLAlchemy sends JSON to PostgreSQL."""

    impl = JSONB
    cache_ok = True

    def process_bind_param(
        self,
        value: dict[str, object] | None,
        dialect: Dialect,
    ) -> dict[str, object] | None:
        del dialect
        if value is not None:
            try:
                ensure_secret_free(value)
            except SecretLeakDetected:
                raise SecretPersistenceError(
                    "Durable payload contains prohibited credential material"
                ) from None
        return value
