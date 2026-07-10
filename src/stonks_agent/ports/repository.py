"""Repository protocols with explicit result values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    key: str
    version: int

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("write receipt key must not be blank")
        if self.version < 1:
            raise ValueError("write receipt version must be positive")


@runtime_checkable
class ReadRepositoryPort[K, T](Protocol):
    def get(self, key: K) -> Result[T]: ...


@runtime_checkable
class WriteRepositoryPort[T](Protocol):
    def put(self, value: T) -> Result[WriteReceipt]: ...
