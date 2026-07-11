"""Exclusive process lock so two scanners do not fight one dongle."""

from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_lock_fh = None


def acquire(path: str | Path = "logs/scanner.lock") -> None:
    global _lock_fh
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w", encoding="utf-8")
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        fh.close()
        raise SystemExit(
            f"Another scanner is already running (lock: {path}).\n"
            "Stop it from the web UI Shutdown button, or: pkill -f 'python -m scanner'"
        ) from exc
    except ImportError:
        # Non-POSIX fallback: best-effort pid file only
        pass
    fh.write(str(os.getpid()))
    fh.flush()
    _lock_fh = fh
    atexit.register(release)
    log.info("Acquired scanner lock %s", path)


def release() -> None:
    global _lock_fh
    if _lock_fh is None:
        return
    try:
        import fcntl

        fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        _lock_fh.close()
    except Exception:
        pass
    _lock_fh = None
