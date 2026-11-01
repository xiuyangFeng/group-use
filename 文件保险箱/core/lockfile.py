"""跨进程互斥锁：防止两个窗口（或 GUI + 命令行）同时操作同一个保险箱。

用操作系统文件锁实现：
- macOS / Linux：fcntl.flock（进程崩溃时内核自动释放，不会留下死锁）
- Windows：msvcrt.locking

同一进程内可重入（计数），方便 GUI 在操作内部再开子操作。
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import VaultStateError

IS_WINDOWS = os.name == "nt"


class VaultLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fd: int | None = None
        self._depth = 0
        self._shared = False

    def acquire(self, shared: bool = False) -> None:
        if self._depth > 0:
            self._depth += 1
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if IS_WINDOWS:
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise VaultStateError("另一个窗口正在操作这个保险箱，请等它结束再试（或先关掉另一个窗口）")
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except OSError:
            pass
        self._fd = fd
        self._depth = 1
        self._shared = shared

    def release(self) -> None:
        if self._depth > 1:
            self._depth -= 1
            return
        if self._depth == 0:
            return
        self._depth = 0
        if self._fd is not None:
            try:
                if IS_WINDOWS:
                    import msvcrt

                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None

    def __enter__(self) -> "VaultLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
