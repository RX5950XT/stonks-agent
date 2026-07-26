from __future__ import annotations

from typing import BinaryIO, cast

from stonks_agent.adapters.artifacts import _file_lock


class _FakeStream:
    def __init__(self) -> None:
        self.offsets: list[int] = []

    def seek(self, offset: int) -> int:
        self.offsets.append(offset)
        return offset

    def fileno(self) -> int:
        return 17


class _FakeMsvcrt:
    LK_NBLCK = 1
    LK_UNLCK = 2

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int]] = []

    def locking(self, file_descriptor: int, mode: int, byte_count: int) -> None:
        self.calls.append((file_descriptor, mode, byte_count))


def test_windows_lock_uses_typed_runtime_adapter(monkeypatch) -> None:
    stream = _FakeStream()
    msvcrt = _FakeMsvcrt()
    monkeypatch.setattr(_file_lock.os, "name", "nt")
    monkeypatch.setattr(_file_lock, "_msvcrt_module", lambda: msvcrt)

    _file_lock._try_lock(cast(BinaryIO, stream))
    _file_lock._release(cast(BinaryIO, stream))

    assert stream.offsets == [0, 0]
    assert msvcrt.calls == [(17, msvcrt.LK_NBLCK, 1), (17, msvcrt.LK_UNLCK, 1)]
