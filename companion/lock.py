"""A cross-process lock guarding every read-modify-write of companion state.

Chrome starts a fresh native-host process for each `sendNativeMessage` call, so
two downloads that complete at the same moment run in two separate processes
against the same state files. With one active call that race was narrow. With
several active calls it is routine, so every mutation of the active-call records
and the pending-download pool must hold this lock.

The lock is an exclusive byte-range lock on a file handle. The operating system
releases it when the handle closes, including when the process dies, so a
crashed companion cannot wedge the system.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

LOCK_TIMEOUT_SECONDS = 15.0
_RETRY_SECONDS = 0.02


@contextmanager
def state_lock(root: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold the companion state lock for the duration of the block."""
    path = Path(root).resolve() / "state" / ".state.lock"
    with _file_lock(path, timeout):
        yield


@contextmanager
def exchange_lock(exchange: Path, timeout: float = 60.0) -> Iterator[None]:
    """Serialize expensive operations on one exchange without locking others."""
    with _file_lock(exchange / "validation" / ".operation.lock", timeout):
        yield


@contextmanager
def _file_lock(path: Path, timeout: float) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        _acquire(handle, timeout)
        try:
            yield
        finally:
            _release(handle)
    finally:
        handle.close()


def _acquire(handle, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            _lock_once(handle)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "another companion process held the state lock for too long"
                ) from None
            time.sleep(_RETRY_SECONDS)


if os.name == "nt":
    import msvcrt

    def _lock_once(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _release(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_once(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
