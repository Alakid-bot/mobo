from __future__ import annotations

import os
from pathlib import Path
from typing import IO


class InstanceLock:
    """Process-lifetime advisory lock protecting a single SQLite volume."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._handle: IO[str] | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle: IO[str] | None = None
        try:
            handle = self.path.open("a+", encoding="utf-8")
            handle.seek(0)
            if not handle.read(1):
                handle.write("0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if handle is not None:
                handle.close()
            raise RuntimeError(
                "检测到另一个 mobo 实例正在使用同一数据库目录；SQLite 模式只允许一个副本"
            ) from exc
        assert handle is not None
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None
