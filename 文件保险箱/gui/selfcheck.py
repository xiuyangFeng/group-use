"""打包后的自检：不依赖源码目录和测试包，验证加密核心 + Qt 界面真的能工作。

用法（安装包里的可执行文件同样支持）：
    QT_QPA_PLATFORM=offscreen 文件保险箱 --selftest
输出结尾 "SELFCHECK PASS：N 项"，退出码 0；失败打印原因、退出码 1。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import traceback
from pathlib import Path

from core.console import hard_exit

PASSWORD = "Selfcheck-Password-1234"
_ok = 0
_failed: list[str] = []
_lines: list[str] = []
_report_path: str | None = None


def _say(text: str) -> None:
    """先存到列表（Windows --windowed 没有 stdout），再尽量打印。"""
    _lines.append(text)
    try:
        print(text, flush=True)
    except Exception:  # noqa: BLE001  某些 windowed 环境 stdout 为 None
        pass


def _check(name: str, fn) -> None:
    global _ok
    try:
        fn()
    except Exception:  # noqa: BLE001
        _failed.append(name)
        _say(f"  ✗ {name}")
        _say(traceback.format_exc())
    else:
        _ok += 1
        _say(f"  ✓ {name}")


def _write_tree(root: Path) -> None:
    (root / "sub").mkdir(parents=True)
    (root / "空目录" / "子目录").mkdir(parents=True)
    (root / "患者名单.txt").write_text("张三\n李四\n", encoding="utf-8")
    (root / "sub" / "空格 和#号.bin").write_bytes(bytes(range(256)) * 40)
    (root / "空文件.txt").write_bytes(b"")


def _digest(root: Path) -> dict:
    from core import constants as C

    out = {}
    skip = {C.VAULT_DIRNAME, C.MARKER_NAME}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if rel in skip or rel.startswith(C.VAULT_DIRNAME + "/"):
            continue
        if p.is_dir():
            out[rel] = ("dir", None)
        else:
            out[rel] = ("file", hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def _finish(code: int) -> int:
    if _report_path:
        try:
            out = Path(_report_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n".join(_lines) + "\n", encoding="utf-8")
        except OSError:
            pass
    return code


def run(report_path: str | None = None) -> int:
    global _report_path
    _report_path = report_path
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("FDVAULT_KDF_FAST", "1")
    os.environ.setdefault("FDVAULT_SKIP_BUSY_CHECK", "1")

    from PySide6.QtWidgets import QApplication

    from core.audit import AuditLog
    from core.control import OperationControl
    from core.errors import VaultStateError
    from core.vault import Vault

    app = QApplication.instance() or QApplication([])

    from gui.dialogs import PasswordDialog, ProgressDialog
    from gui.main_window import MainWindow
    from gui.store import AppStore
    from gui.style import apply_theme, make_app_icon

    apply_theme(app)
    tmp = Path(tempfile.mkdtemp(prefix="fdvault-selfcheck-"))
    cfg = tmp / "cfg"
    cfg.mkdir()
    root = tmp / "数据"
    root.mkdir()
    _write_tree(root)
    before = _digest(root)

    _say("== 加密核心 ==")

    def _roundtrip():
        v = Vault(root, audit_dir=cfg)
        v.create(PASSWORD)
        assert Vault(root).status()["state"] == "LOCKED"
        v.close()
        v = Vault(root, audit_dir=cfg)
        v.unlock(PASSWORD)
        assert _digest(root) == before
        v.lock()
        v.close()
        assert Vault(root).status()["state"] == "LOCKED"

    _check("创建→解锁→上锁，文件字节一致", _roundtrip)

    def _protected():
        from core import protect

        if not protect.enabled():
            return
        assert Vault(root).status()["protected"], "上锁后没有加上防移动/删除保护"
        leaked = protect.probe(root, tmp)
        assert not leaked, f"上锁后这些操作没被拦住：{leaked}"

    _check("上锁后不能移动 / 删除", _protected)

    def _state_machine():
        v = Vault(root, audit_dir=cfg)
        try:
            v.lock()
        except VaultStateError:
            pass
        else:
            raise AssertionError("已上锁时 lock 应该报错")
        finally:
            v.close()

    _check("状态机拒绝错误操作", _state_machine)

    def _audit_chain():
        log = AuditLog(cfg / "audit.jsonl")
        ok, count, detail = log.verify()
        assert ok and count >= 2, (ok, count, detail)

    _check("审计日志哈希链完整", _audit_chain)

    _say("== 图形界面 ==")

    def _window():
        store = AppStore(cfg / "app")
        store.add_vault(root)
        win = MainWindow(store)
        assert len(win.cards) == 1
        assert win.cards[0].info["state"] == "LOCKED"
        win._quitting = True
        win.close()

    _check("主窗口能加载保险箱列表", _window)

    def _dialogs():
        dlg = PasswordDialog(None, str(root))
        dlg.input.setText("")
        dlg._submit()
        assert dlg.result() == 0
        dlg.input.setText("correct-horse")
        dlg._submit()
        assert dlg.result() == 1 and dlg.password == "correct-horse"
        prog = ProgressDialog(None, "正在解锁", "自检", control=OperationControl())
        prog._on_progress({"done_files": 1, "total_files": 4, "done_bytes": 10, "total_bytes": 40})
        assert prog.bar.value() == 25

    _check("密码框 / 进度条可用", _dialogs)

    def _icons():
        assert not make_app_icon().pixmap(64, 64).isNull()

    _check("图标能绘制", _icons)

    from core import protect

    protect.release_tree(tmp)  # 撤掉保护，临时目录才能被系统清理
    _say("")
    if _failed:
        _say(f"SELFCHECK FAIL：{_ok} 通过，{len(_failed)} 失败 -> {_failed}")
        sys.stdout.flush() if sys.stdout else None
        sys.stderr.flush() if sys.stderr else None
        hard_exit(_finish(1))
    _say(f"SELFCHECK PASS：{_ok} 项全部通过")
    hard_exit(_finish(0))
    return 0  # pragma: no cover
