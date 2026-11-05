"""GUI 自检脚本（离屏运行，不弹窗、不依赖屏幕）。

为什么单独一个脚本：PySide6 的 QApplication 在 pytest 解释器退出阶段偶发崩溃，
把 GUI 检查放进子进程并在结尾 os._exit(0) 就可以完全绕开 Qt 的退出析构。

用法：
    QT_QPA_PLATFORM=offscreen FDVAULT_KDF_FAST=1 .venv/bin/python scripts/gui_selftest.py
退出码：0 全部通过；1 有失败（会打印失败项）。
"""

from __future__ import annotations

import faulthandler
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
faulthandler.enable()
os.environ.setdefault("FDVAULT_KDF_FAST", "1")
os.environ.setdefault("FDVAULT_NO_PROTECT", "1")  # 临时目录里的保险箱不加防删除保护，用完能直接删
os.environ.setdefault("FDVAULT_DATA_DIR", __import__("tempfile").mkdtemp(prefix="gui-selftest-data-"))  # 不往真实审计日志里写
os.environ.setdefault("FDVAULT_SKIP_BUSY_CHECK", "1")

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.console import hard_exit, setup_utf8_stdio
from core import constants as C  # noqa: E402
from core.audit import AuditLog  # noqa: E402
from core.control import OperationControl  # noqa: E402
from core.crypto import new_recovery_key, recovery_key_str  # noqa: E402
from core.vault import Vault  # noqa: E402
from tests.conftest import PASSWORD, make_tree, snapshot  # noqa: E402

app = QApplication([])

FAILED: list[str] = []
PASSED = 0


def _hard_exit(code: int) -> None:
    try:
        app.closeAllWindows()
        app.processEvents()
        app.quit()
    except Exception:  # noqa: BLE001
        pass
    hard_exit(code)


def _say(text: str = "") -> None:
    """直接写 fd 1，绕开 Windows/Qt 退出时 flush TextIOWrapper 的 access violation。"""
    try:
        os.write(1, (text + "\n").encode("utf-8", "replace"))
    except OSError:
        pass


def check(name: str, fn) -> None:
    global PASSED
    try:
        fn()
    except Exception:  # noqa: BLE001
        FAILED.append(name)
        _say(f"  ✗ {name}")
        traceback.print_exc()
    else:
        PASSED += 1
        _say(f"  ✓ {name}")


def pump(seconds: float = 0.0) -> None:
    deadline = time.time() + seconds
    while True:
        app.processEvents()
        if time.time() >= deadline:
            break
        time.sleep(0.01)


def make_vault(root: Path, cfg: Path) -> None:
    if not root.exists():
        make_tree(root)
    v = Vault(root, audit_dir=cfg)
    v.create(PASSWORD)
    v.close()


def main() -> int:
    setup_utf8_stdio()
    from gui import main_window as mw
    from gui.dialogs import AuditWindow, PasswordDialog, ProgressDialog, RecoveryDialog, SettingsDialog
    from gui.store import AppStore, idle_seconds
    from gui.style import (
        apply_theme,
        make_app_icon,
        make_tray_icon,
        make_vault_icon,
    )
    from gui.wizard import CreateWizard
    from gui.workers import SingleInstance, VaultOpWorker

    apply_theme(app)
    tmp = Path(tempfile.mkdtemp(prefix="gui-selftest-"))
    cfg = tmp / "cfg"
    cfg.mkdir(parents=True)

    _say("== 基础 ==")

    def _store():
        s = AppStore(cfg / "store1")
        s.add_vault(tmp / "a")
        s.add_vault(tmp / "b")
        s.set_auto_lock(30)
        s2 = AppStore(cfg / "store1")
        assert {Path(p).name for p in s2.vault_paths()} == {"a", "b"}, s2.vault_paths()
        assert s2.auto_lock_minutes == 30
        s2.remove_vault(str(tmp / "a"))
        assert [Path(p).name for p in s2.vault_paths()] == ["b"]

    check("设置存储读写", _store)

    def _idle():
        v = idle_seconds()
        assert v is None or v >= 0, v

    check("空闲时间检测", _idle)

    def _icons():
        assert not make_app_icon().pixmap(64, 64).isNull()
        assert not make_tray_icon().pixmap(32, 32).isNull()
        assert not make_vault_icon(False, 40).isNull()

    check("图标生成", _icons)

    _say("== 主窗口 ==")
    root = tmp / "患者数据"
    root.mkdir()
    make_vault(root, cfg)
    store = AppStore(cfg / "store2")
    store.add_vault(root)
    win = mw.MainWindow(store)
    win.show()
    pump()

    def _cards():
        assert len(win.cards) == 1
        assert win.cards[0].info["state"] == C.LOCKED
        v = Vault(root, audit_dir=cfg)
        v.unlock(PASSWORD)
        v.close()
        win._refresh()
        assert win.cards[0].info["state"] == C.UNLOCKED
        win.cards[0].update_countdown(146)
        assert win.cards[0].countdown.text().startswith("12:34"), win.cards[0].countdown.text()

    check("保险箱卡片状态 / 倒计时", _cards)

    def _missing():
        s = AppStore(cfg / "store3")
        s.add_vault(tmp / "不存在的文件夹")
        w = mw.MainWindow(s)
        assert w.cards[0].info["state"] == "missing"
        w._quitting = True
        w.close()

    check("找不到文件夹的卡片", _missing)

    def _banner():
        s = AppStore(cfg / "store4")
        w = mw.MainWindow(s)
        w.show()
        pump()
        monkey = mw.fsutil.free_bytes
        usage = mw.shutil.disk_usage
        mw.fsutil.free_bytes = lambda _p: 38_600_000_000
        mw.shutil.disk_usage = lambda _p: SimpleNamespace(total=460_000_000_000)
        try:
            w._last_disk_update = 0
            w._update_disk()
            assert w.banner.isVisible()
            assert "磁盘只剩" in w.banner_label.text()
        finally:
            mw.fsutil.free_bytes = monkey
            mw.shutil.disk_usage = usage
            w._quitting = True
            w.close()

    check("低磁盘警告横幅", _banner)

    def _auto_lock():
        target = tmp / "自动锁数据"
        make_vault(target, cfg)
        s = AppStore(cfg / "store5")
        s.set_auto_lock(5)
        w = mw.MainWindow(s)
        v = Vault(target, audit_dir=cfg)
        v.unlock(PASSWORD)
        w.sessions[str(target)] = v
        calls: list = []
        old_idle = mw.idle_seconds
        old_run = w._run_lock
        mw.idle_seconds = lambda: 301
        w._run_lock = lambda vault, **kw: calls.append(kw)
        try:
            w._refresh()
            w._on_tick()
            assert len(calls) == 1 and calls[0].get("auto") is True, calls
        finally:
            mw.idle_seconds = old_idle
            w._run_lock = old_run
            v.close()
            w.sessions.clear()
            w._quitting = True
            w.close()

    check("空闲 5 分钟触发自动上锁", _auto_lock)

    def _auto_lock_skips_in_use():
        import json
        import time as _time

        target = tmp / "脱敏中的数据"
        make_vault(target, cfg)
        s = AppStore(cfg / "store5b")
        s.set_auto_lock(5)
        key = s.add_vault(target)["path"]  # 和真实程序一样，会话用列表里规范化后的路径
        w = mw.MainWindow(s)
        v = Vault(target, audit_dir=cfg)
        v.unlock(PASSWORD)
        w.sessions[key] = v
        marker = target / C.VAULT_DIRNAME / "in-use-ct-deid.json"
        info = {"tool": "CT 数据脱敏工具", "active": True, "heartbeat": _time.time(), "what": "原始数据"}
        marker.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        calls: list = []
        old_idle = mw.idle_seconds
        old_run = w._run_lock
        mw.idle_seconds = lambda: 301
        w._run_lock = lambda vault, **kw: calls.append(kw)
        try:
            w._refresh()
            w._on_tick()
            assert calls == [], f"脱敏工具在用时不应自动上锁：{calls}"
            card = next(c for c in w.cards if c.info["path"] == key)
            assert "正在使用" in card.countdown.text(), card.countdown.text()
            # 脱敏结束（标记改成 active=false）：人还没回来，下一次定时就该锁
            marker.write_text(json.dumps({**info, "active": False}, ensure_ascii=False), encoding="utf-8")
            w._on_tick()
            assert len(calls) == 1 and calls[0].get("auto") is True, calls
        finally:
            mw.idle_seconds = old_idle
            w._run_lock = old_run
            v.close()
            w.sessions.clear()
            w._quitting = True
            w.close()

    check("脱敏工具在用时不自动上锁，用完后照常自动上锁", _auto_lock_skips_in_use)

    _say("== 窗口 / 弹窗 ==")

    def _password():
        dlg = PasswordDialog(None, str(tmp / "x"))
        dlg.show()
        dlg.input.setText("")
        dlg._submit()
        assert dlg.result() == 0 and not dlg.error.isHidden()
        dlg.input.setText("correct-horse")
        dlg._submit()
        assert dlg.password == "correct-horse" and dlg.result() == 1

        key = new_recovery_key()
        dlg2 = PasswordDialog(None, str(tmp / "x"), recovery=True)
        dlg2.show()
        dlg2.input.setText("!!!!")
        dlg2._submit()
        assert dlg2.result() == 0
        dlg2.input.setText(recovery_key_str(key))
        dlg2._submit()
        assert dlg2.recovery_key == key

    check("密码 / 恢复密钥校验", _password)

    def _progress():
        dlg = ProgressDialog(None, "正在解锁", "测试", control=OperationControl())
        dlg._on_progress(
            {
                "phase": "正在解密文件",
                "done_files": 25,
                "total_files": 100,
                "done_bytes": 1_048_576,
                "total_bytes": 4_194_304,
            }
        )
        assert dlg.bar.value() == 25 and dlg.percent.text() == "25%"
        assert dlg.files.text() == "25 / 100 个文件"
        assert "正在解密文件" in dlg.state_label.text()
        dlg._toggle_pause()
        assert dlg.control.paused and dlg.pause_btn.text() == "继续"
        dlg._toggle_pause()
        assert not dlg.control.paused

    check("进度条 / 暂停", _progress)

    def _recovery_dialog():
        dlg = RecoveryDialog(None, str(root), "待恢复（上锁中断）")
        dlg.input.setText("pw")
        dlg._submit()
        assert dlg.password == "pw" and dlg.rollback is False
        dlg2 = RecoveryDialog(None, str(root), "待恢复")
        dlg2.radio_rollback.setChecked(True)
        dlg2.input.setText("pw")
        dlg2._submit()
        assert dlg2.rollback is True

    check("恢复向导选择继续/回滚", _recovery_dialog)

    def _settings():
        s = AppStore(cfg / "store6")
        dlg = SettingsDialog(None, s)
        dlg.minutes.setCurrentIndex(dlg.minutes.findData(30))
        dlg.tray_check.setChecked(False)
        dlg._save()
        assert s.auto_lock_minutes == 30 and s.minimize_to_tray is False

    check("设置保存", _settings)

    def _audit():
        audit_store = AppStore(cfg)
        aw = AuditWindow(audit_store)
        aw.refresh()
        assert aw.table.rowCount() >= 1, aw.table.rowCount()
        ok, count, detail = AuditLog(audit_store.audit_path).verify()
        assert ok and count >= 1, (ok, count, detail)
        aw.close()

    check("审计窗口加载 + 哈希链校验", _audit)

    _say("== 向导端到端 ==")

    def _wizard():
        s = AppStore(cfg / "store7")
        target = tmp / "论文数据-2025"
        target.mkdir()
        make_tree(target)
        before = snapshot(target)

        wiz = CreateWizard(None, s)
        wiz.show()
        wiz.page_folder.path_edit.setText(str(target))
        pump()
        assert wiz.page_folder.isComplete()
        wiz.next()
        pump()
        assert wiz.currentId() == wiz.PAGE_PASSWORD
        # 密码框实时核对：不合格时要立刻显示缺哪条，而不是一直空白等待
        wiz.page_password.pw1.setText("short")
        wiz.page_password.pw2.setText("short")
        pump()
        assert not wiz.page_password.isComplete()
        assert "8 位以上" in wiz.page_password.rules.text() and "○" in wiz.page_password.rules.text()
        assert "还没满足" in wiz.page_password.hint.text()
        wiz.page_password.pw1.setText(PASSWORD)
        wiz.page_password.pw2.setText(PASSWORD)
        pump()
        assert wiz.page_password.isComplete()
        assert "✓ 两次一致" in wiz.page_password.hint.text()
        wiz.next()
        pump()
        assert wiz.currentId() == wiz.PAGE_RECOVERY
        assert wiz.prepared is not None
        assert len(wiz.prepared["recovery_key"].replace("-", "")) == 32
        wiz.page_recovery.check.setChecked(True)
        wiz.next()
        pump(0.05)
        deadline = time.time() + 60
        while not wiz.created and time.time() < deadline:
            pump(0.02)
        assert wiz.created, "首次上锁没有完成"
        assert Vault(target).status()["state"] == C.LOCKED
        wiz._set_buttons_locked(False)
        wiz.accept()
        v = Vault(target, audit_dir=cfg)
        v.unlock(PASSWORD)
        v.close()
        assert snapshot(target) == before

    check("新建向导：选文件夹→密码→恢复密钥→首次上锁→数据完整", _wizard)

    _say("== 后台线程 ==")

    def _worker():
        target = tmp / "线程数据"
        target.mkdir()
        make_tree(target)
        before = snapshot(target)
        make_vault(target, cfg)
        results: list = []
        w = VaultOpWorker(Vault(target, audit_dir=cfg), "unlock", password=PASSWORD)
        w.succeeded.connect(lambda r: results.append("ok"))
        w.failed.connect(lambda n, m: results.append(("fail", m)))
        w.start()
        assert w.wait(60_000)
        pump()
        assert results == ["ok"], results
        assert snapshot(target) == before

        results.clear()
        w2 = VaultOpWorker(Vault(target, audit_dir=cfg), "lock", password=PASSWORD)
        w2.succeeded.connect(lambda r: results.append("ok"))
        w2.failed.connect(lambda n, m: results.append(("fail", m)))
        w2.start()
        assert w2.wait(60_000)
        pump()
        assert results == ["ok"], results
        assert Vault(target).status()["state"] == C.LOCKED

    check("后台线程解锁/上锁", _worker)

    def _worker_cancel():
        target = tmp / "取消数据"
        target.mkdir()
        make_tree(target)
        before = snapshot(target)
        make_vault(target, cfg)
        control = OperationControl()
        control.cancel_after = 2
        events: list = []
        w = VaultOpWorker(Vault(target, audit_dir=cfg), "unlock", password=PASSWORD, control=control)
        w.cancelled.connect(lambda: events.append("cancelled"))
        w.start()
        assert w.wait(60_000)
        pump()
        assert events == ["cancelled"], events
        assert Vault(target).status()["state"] == C.UNLOCKING

        rb = VaultOpWorker(Vault(target, audit_dir=cfg), "recover", password=PASSWORD, rollback=True)
        done: list = []
        rb.succeeded.connect(lambda r: done.append(r))
        rb.start()
        assert rb.wait(60_000)
        pump()
        assert done
        assert Vault(target).status()["state"] == C.LOCKED
        v = Vault(target, audit_dir=cfg)
        v.unlock(PASSWORD)
        v.close()
        assert snapshot(target) == before

    check("取消解锁→自动回滚→数据完整", _worker_cancel)

    def _single():
        name = f"fdvault-selftest-{os.getpid()}"
        first = SingleInstance(name)
        second = SingleInstance(name)
        triggered: list = []
        assert first.start_server(lambda: triggered.append(1))

        # 在子线程里发起连接：Windows 的命名管道在同一线程里“连接 + 等服务端事件”
        # 容易互相等待，用线程更接近真实客户端/服务端两个进程的场景。
        import threading

        def _poke() -> None:
            time.sleep(0.1)
            second.activate_existing()

        threading.Thread(target=_poke, daemon=True).start()
        deadline = time.time() + 8
        while not triggered and time.time() < deadline:
            pump(0.02)
        assert triggered == [1], triggered
        if first.server is not None:
            first.server.close()

    check("单实例（第二个进程唤起已有窗口）", _single)

    def _folder_open():
        from PySide6.QtCore import QEvent, QUrl

        from gui.app import folder_arg, folder_from_event

        target = tmp / "拖拽打开的文件夹"
        target.mkdir()
        expect = str(target.resolve())

        class FakeFileOpen:
            def type(self):
                return QEvent.FileOpen

            def url(self):
                return QUrl.fromLocalFile(str(target))

        # 1) macOS 拖到图标 / open -a：QFileOpenEvent 解析
        assert folder_from_event(FakeFileOpen()) == expect
        # 2) 不是打开文件夹的事件：不处理
        assert folder_from_event(object()) is None
        # 3) Windows 右键菜单 / 命令行：argv 解析（跳过 macOS 的 -psn_xxx）
        assert folder_arg(["app", "-psn_0_123", str(target)]) == expect
        assert folder_arg(["app"]) is None

    check("拖文件夹到图标 / open -a 传路径（QFileOpenEvent）", _folder_open)

    def _leftover_warning():
        target = tmp / "上次没锁回"
        make_vault(target, cfg)
        v = Vault(target, audit_dir=cfg)
        v.unlock(PASSWORD)
        v.close()
        s = AppStore(cfg / "store9")
        s.add_vault(target)
        w = mw.MainWindow(s)
        w._refresh()
        assert "上次没上锁" in w.status_left.text(), w.status_left.text()
        assert "没锁回" in w.cards[0].meta_label.text(), w.cards[0].meta_label.text()
        w._quitting = True
        w.close()

    check("上次没上锁的提醒（状态栏 + 卡片）", _leftover_warning)

    _say("== 新增流程 ==")
    from gui import dialogs as dl
    from gui.wizard import folder_problem

    def _patch(obj, name, value, saved):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def _restore(saved):
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)

    def _wrong_password_retry():
        target = tmp / "输错密码"
        make_tree(target)
        make_vault(target, cfg)
        s = AppStore(cfg / "store10")
        s.add_vault(target)
        w = mw.MainWindow(s)
        answers = iter(["Wrong-pass-1", PASSWORD])
        seen_errors: list = []
        saved: list = []

        def fake_ask(parent, path, *, action="unlock", error="", note=""):
            seen_errors.append(error)
            return next(answers), False

        _patch(mw.PasswordDialog, "ask", staticmethod(fake_ask), saved)
        _patch(mw.QDesktopServices, "openUrl", staticmethod(lambda *_a: True), saved)
        try:
            w.unlock_vault(str(target.resolve()))
            assert seen_errors == ["", "密码不对，再试一次"], seen_errors
            assert Vault(target).status()["state"] == C.UNLOCKED
            assert str(target.resolve()) in w.sessions
            # 上锁不需要再输密码
            w.lock_vault(str(target.resolve()))
            assert Vault(target).status()["state"] == C.LOCKED
            assert not w.sessions
        finally:
            _restore(saved)
            w._quitting = True
            w.close()

    check("输错密码：不弹报错框，直接重输", _wrong_password_retry)

    def _password_management():
        target = tmp / "密码管理"
        make_tree(target)
        v = Vault(target, audit_dir=cfg)
        created = v.create(PASSWORD)
        v.close()
        rec_key = created["recovery_key"]
        from core.crypto import recovery_key_parse

        s = AppStore(cfg / "store11")
        s.add_vault(target)
        w = mw.MainWindow(s)
        path = str(target.resolve())
        saved: list = []
        try:
            # 1) 修改密码：先输错原密码，再输对
            change_answers = iter([("Wrong-Old-1", "NewPass2026a"), (PASSWORD, "NewPass2026a")])
            change_errors: list = []

            def fake_change(parent, p, *, error=""):
                change_errors.append(error)
                return next(change_answers)

            _patch(mw.ChangePasswordDialog, "ask", staticmethod(fake_change), saved)
            w.change_password(path)
            assert change_errors == ["", "原密码不对"], change_errors
            v = Vault(target, audit_dir=cfg)
            v.verify(password="NewPass2026a")
            v.close()

            # 2) 忘记密码：用恢复密钥重置
            reset_answers = iter([(recovery_key_parse(rec_key), "Reset2026Pass")])
            _patch(mw.ResetPasswordDialog, "ask", staticmethod(lambda parent, p, *, error="": next(reset_answers)), saved)
            assert w.reset_password(path) == "Reset2026Pass"
            v = Vault(target, audit_dir=cfg)
            v.verify(password="Reset2026Pass")
            v.close()

            # 3) 换新恢复密钥：展示新密钥的对话框必须勾选才能关
            shown: list = []
            _patch(mw.PasswordDialog, "ask", staticmethod(lambda parent, p, **kw: ("Reset2026Pass", False)), saved)

            class FakeKeyDialog:
                def __init__(self, parent, key, fp):
                    shown.append(key)

                def exec(self):
                    return 1

            _patch(mw, "NewRecoveryKeyDialog", FakeKeyDialog, saved)
            w.regenerate_recovery(path)
            assert len(shown) == 1 and shown[0] != rec_key
            v = Vault(target, audit_dir=cfg)
            v.verify(recovery_key=recovery_key_parse(shown[0]))
            v.close()

            real = dl.NewRecoveryKeyDialog(None, shown[0], "fp")
            real.show()
            assert not real.ok.isEnabled()
            real.reject()
            assert real.isVisible(), "没勾选「已抄好」就被关掉了"
            real.check.setChecked(True)
            assert real.ok.isEnabled()
            real.accept()

            events = [r.get("event") for r in AuditLog(s.audit_path).records()]
            for ev in ("CHANGE_PASSWORD", "RESET_BY_RECOVERY", "REGENERATE_RECOVERY"):
                assert ev in events, (ev, events[-10:])
            assert dl._event_name("RESET_BY_RECOVERY") == "重置密码"
        finally:
            _restore(saved)
            w._quitting = True
            w.close()

    check("修改密码 / 忘记密码重置 / 换恢复密钥", _password_management)

    def _add_existing_and_skip():
        existing = tmp / "别人建的保险箱"
        make_tree(existing)
        make_vault(existing, cfg)
        s = AppStore(cfg / "store12")
        w = mw.MainWindow(s)
        try:
            assert w.empty.isVisibleTo(w) or not s.vaults
            w.new_vault(str(existing))  # 已是保险箱：直接加进列表，不开向导
            assert [Path(p).name for p in s.vault_paths()] == ["别人建的保险箱"]
            w.new_vault(str(existing))  # 再拖一次：不重复
            assert len(s.vault_paths()) == 1
        finally:
            w._quitting = True
            w.close()

        plain = tmp / "新文件夹-跳过第一步"
        make_tree(plain)
        assert folder_problem(plain) is None
        assert folder_problem(existing) == "这个文件夹已经是保险箱"
        assert folder_problem(Path.home()) is not None
        wiz = CreateWizard(None, AppStore(cfg / "store13"), initial_path=str(plain))
        wiz.show()
        pump()
        assert wiz.currentId() == wiz.PAGE_PASSWORD, wiz.currentId()
        assert "新文件夹-跳过第一步" in wiz.page_password.folder_label.text()
        wiz.reject()

    check("拖入已有保险箱直接添加 / 选好文件夹直接设密码", _add_existing_and_skip)

    def _lock_fail_keeps_session():
        target = tmp / "上锁失败"
        make_tree(target)
        make_vault(target, cfg)
        s = AppStore(cfg / "store14")
        s.add_vault(target)
        w = mw.MainWindow(s)
        path = str(target.resolve())
        v = Vault(target, audit_dir=cfg)
        v.unlock(PASSWORD)
        w.sessions[path] = v
        try:
            w._finish_lock(path, v, "failed")  # 例如文件还开着：状态仍是已解锁
            assert w.sessions.get(path) is v, "上锁失败后不该丢掉内存里的密钥"
            w.lock_all()
            assert Vault(target).status()["state"] == C.LOCKED
            assert not w.sessions
        finally:
            w._quitting = True
            w.close()

    check("上锁失败保留密钥，之后「全部上锁」不用再输密码", _lock_fail_keeps_session)


    _say("")
    if FAILED:
        _say(f"GUI SELFTEST FAIL：{PASSED} 通过，{len(FAILED)} 失败 -> {FAILED}")
        _hard_exit(1)
    _say(f"GUI SELFTEST PASS：{PASSED} 项全部通过")
    _hard_exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
