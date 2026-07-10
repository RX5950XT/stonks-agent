from __future__ import annotations

from stonks_agent.domain.errors import Result, Success
from stonks_agent.ports.repository import ReadRepositoryPort, WriteReceipt


class ExampleReader:
    def get(self, key: str) -> Result[str]:
        return Success(value=f"value:{key}")


def test_repository_ports_are_runtime_checkable_typed_protocols() -> None:
    reader = ExampleReader()

    assert isinstance(reader, ReadRepositoryPort)
    result = reader.get("one")
    assert isinstance(result, Success)
    assert result.value == "value:one"


def test_write_receipt_is_explicit_instead_of_ambiguous_none() -> None:
    receipt = WriteReceipt(key="one", version=1)

    assert receipt.key == "one"
    assert receipt.version == 1
