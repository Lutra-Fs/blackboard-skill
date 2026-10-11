from __future__ import annotations

import fcntl
import json
import os
import socket
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from .archive_io import atomic_write_json, safe_archive_path, utc_now_iso


LOCK_CONFLICT_EXIT_CODE = 75


class LockBusyError(RuntimeError):
    """Raised when another writer currently owns a course archive lock."""

    def __init__(self, root: Path, owner: dict[str, Any] | None = None) -> None:
        self.root = Path(root)
        self.owner = owner or {}
        operation = self.owner.get("operation") or "another operation"
        pid = self.owner.get("pid")
        holder = f" (pid {pid})" if isinstance(pid, int) else ""
        super().__init__(f"Course archive is locked by {operation}{holder}: {self.root}")


class CourseLock(AbstractContextManager["CourseLock"]):
    """Advisory, process-scoped writer lease for one course archive.

    The stable ``writer.lock`` inode is the only source of truth. ``owner.json``
    is diagnostic metadata and may safely remain after an unclean process exit.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        operation: str,
        course_id: str | None = None,
        run_id: str | None = None,
        timeout: float = 0.0,
        poll_interval: float = 0.05,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.operation = str(operation or "write")
        self.course_id = str(course_id or "") or None
        self.run_id = str(run_id or "") or None
        self.timeout = max(0.0, float(timeout))
        self.poll_interval = max(0.01, float(poll_interval))
        self._fd: int | None = None
        self._lock_path: Path | None = None
        self._owner_path: Path | None = None
        self._started_at: str | None = None

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    def acquire(self) -> CourseLock:
        if self.acquired:
            return self
        if not self.root.is_dir():
            raise RuntimeError(f"Course archive root is not a directory: {self.root}")

        sync_dir = safe_archive_path(self.root, "_blackboard/.sync", label="course lock directory")
        sync_dir.mkdir(parents=True, exist_ok=True)
        lock_path = safe_archive_path(self.root, "_blackboard/.sync/writer.lock", label="course lock file")
        owner_path = safe_archive_path(self.root, "_blackboard/.sync/owner.json", label="course lock owner")

        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockBusyError(self.root, self._read_owner(owner_path))
                time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))

        self._fd = fd
        self._lock_path = lock_path
        self._owner_path = owner_path
        self._started_at = utc_now_iso()
        try:
            atomic_write_json(owner_path, self._owner_payload())
        except BaseException:
            self.release()
            raise
        return self

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            if self._owner_path is not None:
                self._owner_path.unlink(missing_ok=True)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def __enter__(self) -> CourseLock:
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()

    def _owner_payload(self) -> dict[str, Any]:
        timestamp = utc_now_iso()
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "course_id": self.course_id,
            "operation": self.operation,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": self._started_at or timestamp,
            "heartbeat_at": timestamp,
        }

    @staticmethod
    def _read_owner(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(value, dict):
            return None
        allowed = {"schema_version", "run_id", "course_id", "operation", "pid", "host", "started_at", "heartbeat_at"}
        return {key: value[key] for key in allowed if key in value}
