"""Cross-process lock that serializes acpx/podman runs on the VDS.

The lock file at `ACPX_LOCK_PATH` is shared between the user-facing claude
flow in `smolevich-ai-bot.py` and the benchmark worker in `model-benchmark.py`. Only
one acpx container is allowed to run at a time — two heavy podman processes
would push a small VDS into swap.
"""
from __future__ import annotations

import errno
import fcntl
import logging
import os
import time
from contextlib import contextmanager
from typing import Iterator

from .config import ACPX_ACTIVE_PATH, ACPX_LOCK_PATH

log = logging.getLogger(__name__)


def _open_lock(path: str) -> int:
    parent = os.path.dirname(path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except Exception:
        pass
    return os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)


@contextmanager
def acpx_lock(timeout: float, holder: str = "") -> Iterator[bool]:
    """Try to take the global acpx lock.

    Yields True if acquired, False if the wait exceeded `timeout`. timeout=0
    is non-blocking — one attempt and out. Caller decides what to do on False
    (skip a benchmark tick, tell the user the system is busy, etc.).
    """
    fd = _open_lock(ACPX_LOCK_PATH)
    acquired = False
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        if acquired:
            try:
                os.ftruncate(fd, 0)
                os.write(fd, f"{holder or os.getpid()} {int(time.time())}\n".encode("utf-8"))
            except Exception:
                pass
            log.info("acpx lock acquired by %s (pid=%s)", holder or "?", os.getpid())
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            log.info("acpx lock released by %s (pid=%s)", holder or "?", os.getpid())
        try:
            os.close(fd)
        except Exception:
            pass


def touch_active(path: str = "") -> None:
    """Mark smolevich-ai-bot as currently busy so benchmark can defer its tick."""
    target = path or ACPX_ACTIVE_PATH
    try:
        parent = os.path.dirname(target) or "."
        os.makedirs(parent, exist_ok=True)
        with open(target, "a"):
            os.utime(target, None)
    except Exception:
        pass


def active_recent(within_sec: int, path: str = "") -> bool:
    """Return True when the active-marker was touched within `within_sec`."""
    target = path or ACPX_ACTIVE_PATH
    try:
        mtime = os.stat(target).st_mtime
    except FileNotFoundError:
        return False
    except Exception:
        return False
    return (time.time() - mtime) <= max(0, within_sec)
