"""Small cross-platform advisory lock for local artifact finalization."""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import BinaryIO, Protocol, cast

_RETRYABLE_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EDEADLK})


class _FcntlModule(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, file_descriptor: int, operation: int) -> None: ...


@contextmanager
def exclusive_file_lock(path: Path, *, timeout_seconds: float = 30.0) -> Iterator[None]:
    """Hold one OS-released lock; process crashes cannot strand ownership."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        _ensure_lock_byte(stream)
        _acquire(stream, timeout_seconds)
        try:
            yield
        finally:
            _release(stream)


def _ensure_lock_byte(stream: BinaryIO) -> None:
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"\0")
        stream.flush()
        os.fsync(stream.fileno())


def _acquire(stream: BinaryIO, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            _try_lock(stream)
            return
        except OSError as error:
            if error.errno not in _RETRYABLE_ERRNOS or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _try_lock(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        return
    fcntl = _fcntl_module()
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl = _fcntl_module()
    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _fcntl_module() -> _FcntlModule:
    return cast(_FcntlModule, import_module("fcntl"))
