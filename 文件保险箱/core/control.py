"""长操作的暂停 / 取消控制（GUI 进度条用；CLI 也可以接）。

设计要点：
- 取消不是"立刻杀进程"，而是在安全点（每个文件开始、每次 manifest 提交前）抛 Cancelled；
  此时磁盘状态一定是可恢复的中间态，GUI 随后调用 recover(rollback=True) 回滚到操作前状态。
- 暂停只阻塞调用方线程（就是执行加密的后台线程），不锁 UI。
"""

from __future__ import annotations

import threading

from .errors import Cancelled


class OperationControl:
    def __init__(self) -> None:
        self._run = threading.Event()
        self._run.set()
        self._cancel = threading.Event()
        self.checked = 0
        # 测试专用：检查到第 N 个文件时自动取消
        self.cancel_after: int | None = None
        # 每检查一次调用一次（测试/调试用）
        self.on_check = None

    # ---------------------------------------------------------------- 外部控制

    def pause(self) -> None:
        self._run.clear()

    def resume(self) -> None:
        self._run.set()

    def cancel(self) -> None:
        self._cancel.set()
        self._run.set()  # 叫醒可能正在暂停等待的线程

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def paused(self) -> bool:
        return not self._run.is_set()

    # ---------------------------------------------------------------- 被操作方调用

    def check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled("操作已取消")
        while not self._run.is_set():
            if self._cancel.wait(0.05):
                raise Cancelled("操作已取消")
        self.checked += 1
        if self.on_check is not None:
            self.on_check(self)
        if self.cancel_after is not None and self.checked >= self.cancel_after:
            self._cancel.set()
            raise Cancelled("操作已取消")
