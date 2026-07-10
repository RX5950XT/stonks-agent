"""SQLAlchemy transaction boundary for canonical repositories."""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.repositories import (
    PostgresEvidenceRepository,
    PostgresWorkflowStore,
)


class PostgresUnitOfWork:
    evidence: PostgresEvidenceRepository
    workflows: PostgresWorkflowStore

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session: Session | None = None
        self._committed = False

    def __enter__(self) -> Self:
        if self._session is not None:
            raise RuntimeError("unit of work is already active")
        self._session = Session(self._engine, expire_on_commit=False)
        self.evidence = PostgresEvidenceRepository(self._session)
        self.workflows = PostgresWorkflowStore(self._session)
        self._committed = False
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self._require_session()
        try:
            if exc_type is not None or not self._committed:
                session.rollback()
        finally:
            session.close()
            self._session = None

    def commit(self) -> None:
        session = self._require_session()
        session.commit()
        self._committed = True

    def rollback(self) -> None:
        session = self._require_session()
        session.rollback()
        self._committed = False

    def _require_session(self) -> Session:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        return self._session
