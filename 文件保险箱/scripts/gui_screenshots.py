"""用离屏 Qt 渲染真实界面截图到 docs/images/gui/（不依赖屏幕，可重复生成）。

用法：
    QT_QPA_PLATFORM=offscreen FDVAULT_KDF_FAST=1 .venv/bin/python scripts/gui_screenshots.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FDVAULT_KDF_FAST", "1")
os.environ.setdefault("FDVAULT_NO_PROTECT", "1")  # 临时目录里的保险箱不加防删除保护，用完能直接删

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu  # noqa: E402

from core.vault import Vault  # noqa: E402
from tests.conftest import PASSWORD, make_tree  # noqa: E402

OUT = Path(os.environ.get("FDVAULT_SHOTS_OUT") or (PROJECT / "docs" / "images" / "gui"))
TMP = Path(tempfile.mkdtemp(prefix="fdvault-shots-"))


def shot(widget, name: str, *, resize: tuple[int, int] | None = None) -> Path:
    if resize:
        widget.resize(*resize)
    widget.show()
    app.processEvents()
    for _ in range(3):
        app.processEvents()
    path = OUT / name
    widget.grab().save(str(path))
    print(f"  ✓ {name:28s} {widget.width()}x{widget.height()}")
    return path


def check_png(path: Path) -> None:
    from PySide6.QtGui import QImage

    img = QImage(str(path))
    assert not img.isNull(), f"截图是空的：{path}"
    colors = set()
    step = max(1, img.width() // 60)
    for x in range(0, img.width(), step):
        for y in range(0, img.height(), step):
            colors.add(img.pixel(x, y))
    assert len(colors) > 12, f"截图疑似空白（颜色太少）：{path} {len(colors)}"
    print(f"  · 校验 {path.name}: {len(colors)} 种颜色")


def make_vault(root: Path, cfg: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    make_tree(root)
    # 加一点文件让界面数字好看
    for i in range(12):
        (root / f"样本-{i + 1:02d}.txt").write_text(f"演示数据 {i}\n" * 40, "utf-8")
    v = Vault(root, audit_dir=cfg)
    v.create(PASSWORD)
    v.close()


def main() -> int:
    global app
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    from gui import main_window as mw
    from gui.dialogs import (
        AuditWindow,
        ChangePasswordDialog,
        NewRecoveryKeyDialog,
        PasswordDialog,
        ResetPasswordDialog,
        ProgressDialog,
        RecoveryDialog,
        SettingsDialog,
    )
    from gui.store import AppStore
    from gui.style import apply_theme, make_app_icon, make_tray_icon, make_vault_icon
    from gui.wizard import CreateWizard
    from core.control import OperationControl

    apply_theme(app)
    cfg = TMP / "cfg"
    cfg.mkdir(parents=True, exist_ok=True)

    v1 = TMP / "患者影像-2024"
    v2 = TMP / "组会材料"
    make_vault(v1, cfg)
    make_vault(v2, cfg)

    # 空列表（第一次打开）
    empty_win = mw.MainWindow(AppStore(cfg / "empty"))
    shot(empty_win, "00b-main-empty.png", resize=(760, 560))
    empty_win._quitting = True
    empty_win.close()

    store = AppStore(cfg)
    store.add_vault(v1)
    store.add_vault(v2)

    # 让倒计时显示得像真实场景（否则离屏环境空闲时间可能是 0）
    mw.idle_seconds = lambda: 754  # 12:34

    win = mw.MainWindow(store)
    shot(win, "01-main-locked.png", resize=(760, 560))

    # 解锁一个，展示「已解锁」卡片和倒计时（放进 sessions，模拟是通过界面解锁的）
    vault2 = Vault(v2, audit_dir=cfg)
    vault2.unlock(PASSWORD)
    win.sessions[str(v2)] = vault2
    win._refresh()
    win._on_tick()
    shot(win, "07-main-unlocked.png")

    # 低磁盘警告横幅
    old_free = mw.fsutil.free_bytes
    old_usage = mw.shutil.disk_usage
    mw.fsutil.free_bytes = lambda _p: 38_600_000_000
    mw.shutil.disk_usage = lambda _p: SimpleNamespace(total=460_000_000_000)
    win._last_disk_update = 0
    win._update_disk()
    shot(win, "01b-main-lowdisk.png")
    mw.fsutil.free_bytes = old_free
    mw.shutil.disk_usage = old_usage
    win._last_disk_update = 0
    win._update_disk()

    # ---------------- 新建向导 ----------------
    target = TMP / "论文数据-2025"
    target.mkdir(parents=True, exist_ok=True)
    make_tree(target)
    wiz = CreateWizard(None, store)
    wiz.show()
    app.processEvents()
    wiz.page_folder.path_edit.setText(str(target))
    app.processEvents()
    shot(wiz, "02-wizard-folder.png", resize=(520, 500))

    wiz.next()
    app.processEvents()
    wiz.page_password.pw1.setText(PASSWORD)
    wiz.page_password.pw2.setText(PASSWORD)
    app.processEvents()
    shot(wiz, "03-wizard-password.png")

    wiz.next()  # prepare：生成密钥（测试用低强度 KDF，很快）
    app.processEvents()
    shot(wiz, "04-wizard-recovery.png")

    # 用假进度渲染「首次上锁」页面，避免真的跑完
    wiz.page_recovery.check.setChecked(True)
    app.processEvents()
    wiz.begin_initial_lock = lambda page: page.update_progress(  # type: ignore[assignment]
        {"done_files": 187, "total_files": 480, "done_bytes": 40 * 1024 * 1024, "total_bytes": 100 * 1024 * 1024}
    )
    wiz.next()
    app.processEvents()
    shot(wiz, "05-first-lock.png")
    wiz.reject()  # 撤销未开始的创建（恢复为普通文件夹）

    # ---------------- 弹窗 ----------------
    dlg = PasswordDialog(win, str(v1))
    dlg.input.setText("demo-password-123")
    shot(dlg, "06-unlock.png")
    dlg.reject()

    dlg = PasswordDialog(win, str(v1), error="密码不对，再试一次")
    dlg.input.setText("demo-password-123")
    shot(dlg, "06b-unlock-wrong.png")
    dlg.reject()

    reset = ResetPasswordDialog(win, str(v1))
    reset.key_input.setText("ZHD6-6KW4-Q2MN-8XRT-4HPA-J7CE-WB3F-9LKD")
    reset.pair.pw1.setText(PASSWORD)
    reset.pair.pw2.setText(PASSWORD)
    shot(reset, "14-reset-password.png")
    reset.reject()

    change = ChangePasswordDialog(win, str(v1))
    change.old.setText("old-password")
    change.pair.pw1.setText("Short1")
    shot(change, "15-change-password.png")
    change.reject()

    newkey = NewRecoveryKeyDialog(win, "ZHD6-6KW4-Q2MN-8XRT-4HPA-J7CE-WB3F-9LKD", "a1b2c3d4")
    shot(newkey, "16-new-recovery-key.png")
    newkey.check.setChecked(True)
    newkey.accept()

    card_menu = win.cards[0]._build_menu()
    card_menu.popup(QPoint(300, 300))
    app.processEvents()
    shot(card_menu, "10-card-menu.png")
    card_menu.close()

    rec = RecoveryDialog(win, str(v1), "待恢复（上锁中断）")
    shot(rec, "12-recover.png")
    rec.reject()

    settings = SettingsDialog(win, store)
    shot(settings, "13-settings.png")
    settings.reject()

    audit = AuditWindow(store)
    audit.refresh()
    shot(audit, "11-audit.png", resize=(1020, 640))
    print(f"  · 审计记录数：{audit.table.rowCount()}")

    control = OperationControl()
    prog = ProgressDialog(win, "正在上锁「组会材料」", control=control)
    prog._on_progress(
        {
            "done_files": 1400,
            "total_files": 3400,
            "done_bytes": 320 * 1024 * 1024,
            "total_bytes": 2_100_000_000,
            "phase": "正在加密文件",
        }
    )
    shot(prog, "08-progress.png")
    prog.reject()

    # 托盘菜单（macOS 菜单栏的右键菜单内容）
    menu = QMenu()
    for text in ("打开主窗口", "全部上锁", None, "日志", "设置", None, "退出"):
        if text is None:
            menu.addSeparator()
        else:
            menu.addAction(text)
    menu.popup(QPoint(300, 300))
    app.processEvents()
    shot(menu, "09-tray-menu.png")
    menu.close()

    # 图标
    from PySide6.QtGui import QPixmap

    icon_pm = make_app_icon().pixmap(128, 128)
    tray_pm = make_tray_icon().pixmap(64, 64)
    vault_locked = make_vault_icon(False, 64)
    vault_unlocked = make_vault_icon(True, 64)
    combo = QPixmap(360, 128)
    combo.fill(0)
    p = None
    from PySide6.QtGui import QPainter

    p = QPainter(combo)
    p.drawPixmap(8, 0, icon_pm)
    p.drawPixmap(150, 32, tray_pm)
    p.drawPixmap(230, 32, vault_locked)
    p.drawPixmap(300, 32, vault_unlocked)
    p.end()
    combo.save(str(OUT / "00-icons.png"))
    print("  ✓ 00-icons.png")

    print("\n全部截图：")
    for p in sorted(OUT.glob("*.png")):
        check_png(p)
    print(f"\n输出目录：{OUT}")
    print(f"（临时数据：{TMP}，可删除）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
