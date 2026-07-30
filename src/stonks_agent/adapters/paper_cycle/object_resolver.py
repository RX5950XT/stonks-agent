"""Content-addressed durable object resolver for paper-cycle checkpoints."""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ValidationError

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.paper_cycle import CanonicalCycleReference
from stonks_agent.ports.artifact_store import ArtifactReaderPort


class ArtifactPaperCycleObjectResolver:
    __slots__ = ("_artifacts",)

    def __init__(self, artifacts: ArtifactReaderPort) -> None:
        self._artifacts = artifacts

    def resolve[T: BaseModel](
        self,
        reference: CanonicalCycleReference,
        *,
        object_type: type[T],
        object_id: Callable[[T], str],
        semantic_hash: Callable[[T], str],
    ) -> Result[T]:
        loaded = self._artifacts.read(reference.content_hash)
        if isinstance(loaded, Failure):
            return loaded
        try:
            value = object_type.model_validate_json(loaded.value)
            resolved_id = object_id(value)
            resolved_hash = semantic_hash(value)
        except (TypeError, ValueError, ValidationError):
            return _failure(
                ErrorCode.CONFLICT,
                "Paper cycle durable object is invalid",
            )
        if resolved_id != reference.ref_id or resolved_hash != reference.content_hash:
            return _failure(
                ErrorCode.CONFLICT,
                "Paper cycle durable object reference changed",
            )
        return Success(value)


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
