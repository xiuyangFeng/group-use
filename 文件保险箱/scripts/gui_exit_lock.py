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
from tests.conftest import PASSWORD, make_tree  # noqa: E402

app = QApplication([])


def _say(text: str) -> None:
    """直接写 fd 1，绕开 Windows/Qt 退出时 flush 的 access violation。"""
    try:
        os.write(1, (text + "\n").encode("utf-8", "replace"))
    except OSError:
        pass


def main() -> int:
    setup_utf8_stdio()
    QTimer.singleShot(30000, lambda: (print("EXITLOCK TIMEOUT", flush=True), os._exit(3)))
    tmp = Path(tempfile.mkdtemp(prefix="fdvault-exitlock-"))
    root = tmp / "数据"
    make_tree(root)
    v = Vault(root, audit_dir=tmp / "cfg")
    v.create(PASSWORD)
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
    _say("EXITLOCK PASS：退出前自动上锁，明文已清空")
    return 0


if __name__ == "__main__":
    code = main()
    hard_exit(code)
