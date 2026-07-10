"""Vendor-neutral tracing and metrics ports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from stonks_agent.domain.errors import StructuredError


class TraceContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    run_id: str | None = Field(default=None, min_length=1, max_length=128)
    job_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("trace_id", "span_id")
    @classmethod
    def reject_zero_ids(cls, value: str) -> str:
        if set(value) == {"0"}:
            raise ValueError("trace identifiers cannot be all zero")
        return value

    def correlation_attributes(self) -> dict[str, str]:
        values = self.model_dump(exclude_none=True)
        return {str(key): str(value) for key, value in values.items()}


@runtime_checkable
class MetricsPort(Protocol):
    def increment(
        self,
        name: str,
        value: int = 1,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> None: ...

    def observe(
        self,
        name: str,
        value: float,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> None: ...


class SpanPort(Protocol):
    def set_attribute(self, name: str, value: str | int | float | bool) -> None: ...

    def record_error(self, error: StructuredError) -> None: ...

    def end(self) -> None: ...


@runtime_checkable
class TracerPort(Protocol):
    def start_span(
        self,
        name: str,
        *,
        parent: TraceContext | None = None,
        attributes: Mapping[str, str] | None = None,
    ) -> SpanPort: ...
