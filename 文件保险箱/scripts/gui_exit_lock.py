"""验收第 11 条：解锁后关窗口/退出时，程序会自动把保险箱锁回去。

为什么单独一个子进程：和 gui_selftest 一样，PySide6 在 pytest 主进程里退出阶段偶发崩溃，
把"建窗口 → 放进会话 → 调退出前上锁"放进子进程，结尾 os._exit，互不污染。

用法：
    QT_QPA_PLATFORM=offscreen FDVAULT_KDF_FAST=1 .venv/bin/python scripts/gui_exit_lock.py
退出码：0 通过；1 失败；3 超时。
"""

from __future__ import annotations

import faulthandler
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FDVAULT_KDF_FAST", "1")
os.environ.setdefault("FDVAULT_NO_PROTECT", "1")  # 临时目录里的保险箱不加防删除保护，用完能直接删
os.environ.setdefault("FDVAULT_SKIP_BUSY_CHECK", "1")
faulthandler.enable()

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.console import hard_exit, setup_utf8_stdio
from core import constants as C  # noqa: E402
from core.vault import Vault  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402
from gui.store import AppStore  # noqa: E402
from tests.conftest import PASSWORD, QUESTIONS, make_tree  # noqa: E402

app = QApplication([])


def _say(text: str) -> None:
    """直接写 fd 1，绕开 Windows/Qt 退出时 flush 的 access violation。"""
    try:
        os.write(1, (text + "\n").encode("utf-8", "replace"))
    except OSError:
        pass


def main() -> int:
    setup_utf8_stdio()
    QTimer.singleShot(30000, lambda: (print("EXITLOCK TIMEOUT", flush=True), hard_exit(3)))
    tmp = Path(tempfile.mkdtemp(prefix="fdvault-exitlock-"))
    root = tmp / "数据"
    make_tree(root)
    v = Vault(root, audit_dir=tmp / "cfg")
    v.create(PASSWORD, QUESTIONS)
    v.close()

    v = Vault(root, audit_dir=tmp / "cfg")
    v.unlock(PASSWORD)
    store = AppStore(tmp / "cfg" / "app")
    store.add_vault(root)
    win = MainWindow(store)
    win.sessions[str(root)] = v
    try:
        failed = win._lock_before_exit()
        assert failed == [], f"上锁失败：{failed}"
        assert not win.sessions, "会话没有被清空"
        assert Vault(root).status()["state"] == C.LOCKED, "退出后状态不是 LOCKED"
        # 上锁后根目录只剩 .vault + 说明文件
        leftovers = [p.name for p in root.iterdir() if p.name not in (C.VAULT_DIRNAME, C.MARKER_NAME)]
        assert not leftovers, f"上锁后还有明文：{leftovers}"
        win._quitting = True
        win.close()
    except Exception:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        _say("EXITLOCK FAIL")
        return 1
    # 第二种退法：系统发来的退出请求（macOS ⌘Q / Dock 退出 / 注销 / 关机先发 QEvent.Close，
    # QApplication.quit() 和 Windows 注销关机发 QEvent.Quit），
    # 窗口缩在托盘里（隐藏）——以前这条路不上锁就直接退了
    try:
        from PySide6.QtCore import QEvent

        from gui.app import QuitGuard

        root2 = tmp / "数据2"
        make_tree(root2)
        v2 = Vault(root2, audit_dir=tmp / "cfg")
        v2.create(PASSWORD, QUESTIONS)
        v2.close()
        v2 = Vault(root2, audit_dir=tmp / "cfg")
        v2.unlock(PASSWORD)
        store2 = AppStore(tmp / "cfg" / "app2")
        store2.add_vault(root2)
        win2 = MainWindow(store2)
        win2.sessions[str(root2)] = v2
        win2.hide()
        guard = QuitGuard(win2)
        app.installEventFilter(guard)

        # 锁不上、用户点「取消」：退出请求要被拒绝，保险箱保持原样
        real_lock = win2._lock_before_exit
        win2._lock_before_exit = lambda: ["数据2"]
        win2._ask_quit_anyway = lambda failed: "cancel"
        for kind in (QEvent.Close, QEvent.Quit):
            ev = QEvent(kind)
            QApplication.sendEvent(app, ev)
            assert not ev.isAccepted(), f"锁不上又选了取消，退出请求（{kind}）应该被拒绝"
        assert not win2._quitting and win2.sessions, "取消后不应该进入退出状态"

        # 正常情况：先上锁，再放行退出（macOS 的 ⌘Q / Dock 退出先发的是 Close）
        win2._lock_before_exit = real_lock
        ev = QEvent(QEvent.Close)
        QApplication.sendEvent(app, ev)
        assert ev.isAccepted(), "上锁成功后应该放行退出"
        assert win2._quitting and not win2.sessions
        assert Vault(root2).status()["state"] == C.LOCKED, "系统退出后状态不是 LOCKED"
        leftovers = [p.name for p in root2.iterdir() if p.name not in (C.VAULT_DIRNAME, C.MARKER_NAME)]
        assert not leftovers, f"系统退出后还有明文：{leftovers}"
        app.removeEventFilter(guard)
    except Exception:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        _say("EXITLOCK FAIL（系统退出请求）")
        return 1
    _say("EXITLOCK PASS：托盘退出和系统退出请求（⌘Q / Dock / 注销 / 关机）都会先上锁，明文已清空")
    return 0


if __name__ == "__main__":
    code = main()
    hard_exit(code)
