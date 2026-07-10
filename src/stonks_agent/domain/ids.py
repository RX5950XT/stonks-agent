"""Validated namespaced identifiers."""

from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)


class EntityId(BaseModel):
    """A UUID with an explicit aggregate namespace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    value: UUID


def new_entity_id(namespace: str) -> EntityId:
    return EntityId(namespace=namespace, value=uuid4())


def parse_entity_id(namespace: str, raw_value: object) -> Result[EntityId]:
    """Parse untrusted ID input without throwing an unstructured error."""

    try:
        parsed = EntityId.model_validate(
            {"namespace": namespace, "value": raw_value},
            strict=False,
        )
    except ValidationError:
        return Failure(
            StructuredError(
                code=ErrorCode.INVALID_INPUT,
                message="Invalid entity identifier",
                details={"field": "id"},
            )
        )
    return Success(parsed)
