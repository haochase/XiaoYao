from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import time
from typing import BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


MAX_LOCK_WAIT_SECONDS = 30.0
_LOCK_RETRY_SECONDS = 0.05


def _state_lock_path(state_path: Path, _project_id: str) -> Path:
    normalized_state = os.path.normcase(str(state_path.resolve(strict=False)))
    lock_key = hashlib.sha256(normalized_state.encode("utf-8")).hexdigest()[:16]
    return state_path.parent / f".dws-sync-state-{lock_key}.lock"


def _try_lock(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _is_lock_contention(error: OSError) -> bool:
    return isinstance(error, BlockingIOError) or error.errno in {
        errno.EACCES,
        errno.EAGAIN,
        errno.EDEADLK,
    }


@contextmanager
def acquire_state_lock(
    state_path: Path,
    project_id: str,
    *,
    timeout: float = MAX_LOCK_WAIT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[Path]:
    lock_path = _state_lock_path(state_path, project_id)
    try:
        stream = lock_path.open("a+b", buffering=0)
    except OSError:
        raise ValueError("private_file_write_failed") from None

    acquired = False
    try:
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            raise ValueError("private_file_write_failed") from None

        wait_seconds = min(max(timeout, 0.0), MAX_LOCK_WAIT_SECONDS)
        deadline = monotonic() + wait_seconds
        first_attempt = True
        while True:
            if not first_attempt and monotonic() >= deadline:
                raise ValueError("sync_lock_timeout")
            first_attempt = False
            try:
                _try_lock(stream)
                acquired = True
                break
            except OSError as exc:
                if not _is_lock_contention(exc):
                    raise ValueError("private_file_write_failed") from None
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise ValueError("sync_lock_timeout") from None
                sleep(min(_LOCK_RETRY_SECONDS, remaining))

        yield lock_path
    finally:
        if acquired:
            try:
                _unlock(stream)
            except OSError:
                pass
        stream.close()
