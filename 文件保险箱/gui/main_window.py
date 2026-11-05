"""主窗口、托盘、自动上锁。"""

from __future__ import annotations

import getpass
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QScrollArea,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from core import constants as C, fsutil
from core import inuse
from core.control import OperationControl
from core.errors import PasswordError, RecoveryKeyError, VaultError
from core.vault import Vault

from .dialogs import (
    AuditWindow,
    ChangePasswordDialog,
    NewRecoveryKeyDialog,
    PasswordDialog,
    ProgressDialog,
    RecoveryDialog,
    ResetPasswordDialog,
    SettingsDialog,
)
from .quit_flow import quit_choices
from .store import AppStore, idle_seconds
from .style import (
    AMBER,
    GREEN,
    GREEN_DOT,
    MUTED,
    button,
    elide,
    file_manager_name,
    hline,
    icon_button,
    label,
    make_app_icon,
    make_tool_icon,
    make_tray_icon,
    make_vault_icon,
    short_path,
    spacer,
)
from .widgets import DropZone, ElidedLabel, dropped_folder
from .wizard import CreateWizard, folder_problem
from .workers import VaultOpWorker

DIALOG_OK = 1
NEEDS_RECOVERY = (C.LOCKING, C.UNLOCKING, C.INCONSISTENT)
STABLE = (C.LOCKED, C.UNLOCKED)


def _state_text(state: str) -> str:
    return {
        C.LOCKED: "已上锁",
        C.UNLOCKED: "已解锁",
        C.LOCKING: "上锁中断",
        C.UNLOCKING: "解锁中断",
        C.INCONSISTENT: "异常",
    }.get(state, state)


def _name(path: str) -> str:
    return Path(path).name or path


class VaultCard(QWidget):
    """白色分组里的一行（系统设置风）。名字沿用 VaultCard，外部代码 / 自检都按这个名字用。"""

    def __init__(self, main: "MainWindow", info: dict):
        super().__init__()
        self.main = main
        self.info = info
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("vaultRow")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 11, 12, 11)
        lay.setSpacing(14)

        state = info["state"]
        path = info["path"]
        kind = {C.LOCKED: "locked", C.UNLOCKED: "unlocked"}.get(state)
        if kind is None:
            kind = "warn" if state in NEEDS_RECOVERY else "error"
        if self._leftover():
            kind = "warn"
        icon = QLabel()
        icon.setFixedSize(38, 38)
        icon.setPixmap(make_vault_icon(state == C.UNLOCKED, 38, kind=kind))
        lay.addWidget(icon)

        meta = QVBoxLayout()
        meta.setSpacing(3)
        name = label(elide(_name(path), 40), role="h2")
        name.setStyleSheet("font-size:14.5px;")
        meta.addWidget(name)

        sub = QHBoxLayout()
        sub.setSpacing(6)
        self.dot = QLabel()
        self.dot.setFixedSize(7, 7)
        self.countdown = QLabel("")
        self.countdown.setProperty("role", "muted")
        if state == C.UNLOCKED and not self._leftover():
            self.dot.setStyleSheet(f"background:{GREEN_DOT};border-radius:3px;")
            sub.addWidget(self.dot, 0, Qt.AlignVCenter)
            self.meta_label = label("已解锁 ·", role="muted")
            sub.addWidget(self.meta_label)
            sub.addWidget(self.countdown)
            sub.addStretch(1)
        else:
            self.dot.hide()
            self.countdown.hide()
            self.meta_label = ElidedLabel(self._sub_text())
            self.meta_label.setProperty("role", "muted")
            if self._leftover() or state in NEEDS_RECOVERY:
                self.meta_label.setStyleSheet(f"color:{AMBER};font-weight:600;")
            sub.addWidget(self.meta_label, 1)
        meta.addLayout(sub)
        lay.addLayout(meta, 1)
        self.setToolTip(self._tooltip())

        btns = QHBoxLayout()
        btns.setSpacing(8)
        if state == C.LOCKED:
            btns.addWidget(button("解锁", on_click=self._unlock))
        elif state == C.UNLOCKED:
            btns.addWidget(button("打开", on_click=self._open_folder))
            btns.addWidget(button("上锁", kind="primary", on_click=self._lock))
        elif state in NEEDS_RECOVERY:
            btns.addWidget(button("恢复", kind="primary", on_click=self._recover))
        else:
            btns.addWidget(button("移除", on_click=self._remove))
        if state != "missing":
            more = button("•••", kind="more")
            more.setToolTip("更多")
            more.setMenu(self._build_menu())
            btns.addWidget(more)
        lay.addLayout(btns)

    # ---------------------------------------------------------------- 文本

    def _leftover(self) -> bool:
        """解锁着，但不是本次程序里解锁的（上次退出时没锁回）。"""
        return self.info["state"] == C.UNLOCKED and self.info["path"] not in self.main.sessions

    def _sub_text(self) -> str:
        info = self.info
        state = info["state"]
        if state == "missing":
            return "文件夹不见了（被移动、改名或删除）"
        if state == "error":
            return f"打不开：{info.get('error', '')}"
        if state in NEEDS_RECOVERY:
            return "上次操作被中断，文件没丢"
        if self._leftover():
            return "上次没锁回，文件还是明文，请上锁"
        files = int(info.get("file_count") or 0)
        total = int(info.get("total_bytes") or 0)
        return f"{short_path(info['path'])} · {files:,} 个文件 · {fsutil.human_bytes(total)}"

    def _tooltip(self) -> str:
        lines = [self.info["path"]]
        ref = self.main.store.get(self.info["path"]) or {}
        if ref.get("last_unlock_at"):
            who = f"（{ref['last_unlock_by']}）" if ref.get("last_unlock_by") else ""
            lines.append(f"上次解锁：{ref['last_unlock_at']}{who}")
        if self.info.get("recovery_fingerprint"):
            lines.append(f"恢复密钥指纹：{self.info['recovery_fingerprint']}")
        return "\n".join(lines)

    def _build_menu(self) -> QMenu:
        state = self.info["state"]
        m = QMenu(self)
        m.addAction(f"在{file_manager_name()}中显示", self._open_folder)
        if state in STABLE:
            m.addSeparator()
            m.addAction("修改密码…", lambda: self.main.change_password(self.info["path"]))
            m.addAction("忘记密码…", lambda: self.main.reset_password(self.info["path"]))
            m.addAction("换新恢复密钥…", lambda: self.main.regenerate_recovery(self.info["path"]))
        if state == C.LOCKED:
            m.addAction("校验完整性…", lambda: self.main.verify_vault(self.info["path"]))
        m.addSeparator()
        m.addAction("从列表移除", self._remove)
        return m

    # ---------------------------------------------------------------- 定时

    def update_countdown(self, idle: float | None) -> None:
        if self.info["state"] != C.UNLOCKED:
            return
        users = inuse.active_users(self.info["path"])
        if users:
            # 脱敏这类长任务跑着的时候，人可能早就离开了电脑；这时不倒计时，也不自动上锁
            self.countdown.setText(f"{inuse.describe(users)}正在使用，用完前不自动上锁")
            return
        minutes = self.main.store.auto_lock_minutes
        if idle is None:
            self.countdown.setText(f"{minutes} 分钟无操作后自动上锁")
            return
        remain = int(max(0, minutes * 60 - idle))
        self.countdown.setText(f"{remain // 60:02d}:{remain % 60:02d} 后自动上锁")

    # ---------------------------------------------------------------- 操作

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        state = self.info["state"]
        if state == C.LOCKED:
            self._unlock()
        elif state == C.UNLOCKED:
            self._open_folder()
        elif state in NEEDS_RECOVERY:
            self._recover()
        super().mouseDoubleClickEvent(event)

    def _unlock(self) -> None:
        self.main.unlock_vault(self.info["path"])

    def _lock(self) -> None:
        self.main.lock_vault(self.info["path"])

    def _recover(self) -> None:
        self.main.recover_vault(self.info["path"])

    def _open_folder(self) -> None:
        path = self.info["path"]
        if Path(path).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        else:
            QMessageBox.warning(self.main, "找不到文件夹", path)

    def _remove(self) -> None:
        if (
            QMessageBox.question(
                self.main,
                "从列表移除",
                f"只是不在列表里显示「{_name(self.info['path'])}」，文件不会删除。",
                QMessageBox.Ok | QMessageBox.Cancel,
                QMessageBox.Ok,
            )
            == QMessageBox.Ok
        ):
            self.main.store.remove_vault(self.info["path"])
            self.main._refresh()


class MainWindow(QMainWindow):
    def __init__(self, store: AppStore):
        super().__init__()
        self.store = store
        self.sessions: dict[str, Vault] = {}
        self._protect_tried: set[str] = set()
        self.cards: list[VaultCard] = []
        self._busy = False
        self._quitting = False
        self._quit_in_progress = False
        self._auto_locking = False
        self._auto_lock_blocked = False  # 自动上锁失败后，等有人动了键盘鼠标再试，避免每秒弹一次
        self._last_disk_update = 0.0
        self._last_refresh = 0.0
        self._free_bytes = 0
        self._free_pct = 100.0
        self.audit_window: AuditWindow | None = None
        self.tray: QSystemTrayIcon | None = None

        self.setWindowTitle("文件保险箱")
        self.setWindowIcon(make_app_icon())
        self.resize(760, 560)
        self.setMinimumSize(620, 420)
        self.setAcceptDrops(True)

        self._build_ui()
        self._setup_shortcuts()
        self._setup_tray()
        self._refresh()

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._on_tick)
        self.timer.start()
        self._on_tick()

    # ---------------------------------------------------------------- 界面

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("page")
        root = QVBoxLayout(central)
        root.setContentsMargins(26, 20, 26, 14)
        root.setSpacing(10)

        bar = QHBoxLayout()
        bar.setSpacing(4)
        bar.addWidget(label("保险箱", role="title"))
        bar.addWidget(spacer())
        self.lock_all_btn = button("全部上锁", kind="danger", on_click=self.lock_all)
        self.lock_all_btn.hide()
        bar.addWidget(self.lock_all_btn)
        bar.addSpacing(4)
        bar.addWidget(icon_button(make_tool_icon("log"), "操作日志", self.open_audit))
        bar.addWidget(icon_button(make_tool_icon("gear"), "设置", self.open_settings))
        bar.addSpacing(6)
        self.new_btn = button("＋ 新建", kind="primary", on_click=lambda: self.new_vault())
        self.new_btn.setToolTip("选一个文件夹加密（也可以直接把文件夹拖进窗口）")
        bar.addWidget(self.new_btn)
        root.addLayout(bar)
        root.addSpacing(4)

        self.banner = QFrame()
        self.banner.setProperty("banner", "warn")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(14, 8, 14, 8)
        self.banner_label = label("", wrap=True)
        bl.addWidget(self.banner_label, 1)
        self.banner.hide()
        root.addWidget(self.banner)

        self.drop_banner = QFrame()
        self.drop_banner.setProperty("banner", "info")
        dl = QHBoxLayout(self.drop_banner)
        dl.setContentsMargins(14, 10, 14, 10)
        hint = label("松开鼠标：新文件夹会加密，已有保险箱直接加进列表", role="h2")
        hint.setAlignment(Qt.AlignCenter)
        dl.addWidget(hint, 1)
        self.drop_banner.hide()
        root.addWidget(self.drop_banner)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        self.list_container = QWidget()
        self.list_container.setObjectName("cardList")
        self.list_container.setStyleSheet("QWidget#cardList{background:transparent;}")
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(6)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_container)
        root.addWidget(self.scroll, 1)

        self.empty = DropZone("把要保护的文件夹拖到这里", "选择文件夹", subtitle="加密后只有输对密码才能打开")
        self.empty.clicked.connect(lambda: self.new_vault())
        root.addWidget(self.empty, 1)

        foot = QHBoxLayout()
        self.status_left = label("", role="muted")
        foot.addWidget(self.status_left)
        foot.addWidget(spacer())
        foot.addWidget(label("本机加密 · 不联网", role="muted"))
        root.addLayout(foot)

        self.setCentralWidget(central)

    def _setup_shortcuts(self) -> None:
        for seq, fn in (
            (QKeySequence.New, lambda: self.new_vault()),
            (QKeySequence("Ctrl+L"), self.lock_all),
            (QKeySequence.Preferences, self.open_settings),
            (QKeySequence.Refresh, self._refresh),
        ):
            act = QAction(self)
            act.setShortcut(seq)
            act.triggered.connect(fn)
            self.addAction(act)

    # ---------------------------------------------------------------- 拖放

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if not self._busy and dropped_folder(event.mimeData()):
            event.acceptProposedAction()
            self._show_drop(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if dropped_folder(event.mimeData()):
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._show_drop(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        self._show_drop(False)
        path = dropped_folder(event.mimeData())
        if path:
            event.acceptProposedAction()
            # 拖放事件里不能开模态窗口，放到下一轮事件循环
            QTimer.singleShot(0, lambda p=path: self.new_vault(p))

    def _show_drop(self, active: bool) -> None:
        if self.empty.isVisible():
            self.empty.set_active(active)
        else:
            self.drop_banner.setVisible(active)

    # ---------------------------------------------------------------- 刷新

    def _vault_info(self, path: str) -> dict:
        if not Path(path).is_dir():
            return {"path": path, "state": "missing", "exists": False}
        try:
            vault = Vault(path)
            info = vault.status()
            if info["state"] == C.LOCKED and not info.get("protected") and path not in self._protect_tried:
                # 旧版本上锁的保险箱没有防移动 / 防删除保护：补上（每个只试一次，U 盘等不支持的不反复试）
                self._protect_tried.add(path)
                info["protected"] = vault.ensure_protected()
            info["path"] = path
            info["exists"] = True
            return info
        except (VaultError, OSError) as exc:
            return {"path": path, "state": "error", "exists": True, "error": str(exc)}

    def _refresh(self) -> None:
        self._last_refresh = time.monotonic()
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()  # 先藏起来：deleteLater 要等下一轮事件循环，不藏会和新列表叠在一起
                w.setParent(None)
                w.deleteLater()
        self.cards = []

        vaults = self.store.vaults
        sections: dict[str, list[VaultCard]] = {"正在使用": [], "已上锁": [], "需要处理": []}
        for ref in vaults:
            card = VaultCard(self, self._vault_info(ref["path"]))
            state = card.info["state"]
            if state == C.UNLOCKED or state in NEEDS_RECOVERY:
                sections["正在使用"].append(card)
            elif state == C.LOCKED:
                sections["已上锁"].append(card)
            else:
                sections["需要处理"].append(card)
            self.cards.append(card)
        for title, cards in sections.items():
            if not cards:
                continue
            self.list_layout.insertWidget(self.list_layout.count() - 1, label(title, role="section"))
            group = QFrame()
            group.setProperty("group", True)
            gl = QVBoxLayout(group)
            gl.setContentsMargins(0, 0, 0, 0)
            gl.setSpacing(0)
            for i, card in enumerate(cards):
                if i:
                    sep_row = QHBoxLayout()
                    sep_row.setContentsMargins(66, 0, 0, 0)
                    sep_row.addWidget(hline())
                    gl.addLayout(sep_row)
                gl.addWidget(card)
            self.list_layout.insertWidget(self.list_layout.count() - 1, group)
            self.list_layout.insertSpacing(self.list_layout.count() - 1, 10)

        self.scroll.setVisible(bool(vaults))
        self.empty.setVisible(not vaults)
        self.lock_all_btn.setVisible(bool(self.sessions))
        self._update_status()
        self._update_disk()
        self._update_tray_status()

    def _update_status(self) -> None:
        unlocked = sum(1 for c in self.cards if c.info["state"] == C.UNLOCKED)
        leftover = [c for c in self.cards if c._leftover()]
        if leftover:
            text = f"⚠ {len(leftover)} 个保险箱上次没上锁，文件还是明文"
            style = f"color:{AMBER};font-weight:600;"
        elif unlocked:
            text = f"{unlocked} 个已解锁 · 用完记得上锁"
            style = f"color:{GREEN};"
        else:
            text, style = "拖文件夹到窗口即可加密", f"color:{MUTED};"
        self.status_left.setStyleSheet(style)
        self.status_left.setText(text)

    def _update_disk(self) -> None:
        now = time.monotonic()
        if now - self._last_disk_update > 30 or self._last_disk_update == 0:
            self._last_disk_update = now
            try:
                free = fsutil.free_bytes(Path.home())
                total = shutil.disk_usage(str(Path.home())).total
                self._free_bytes = free
                self._free_pct = free * 100.0 / total if total else 100.0
            except OSError:
                self._free_bytes, self._free_pct = 0, 100.0
        if self._free_pct < 15:
            self.banner_label.setText(
                f"磁盘只剩 {self._free_pct:.1f}%（{fsutil.human_bytes(self._free_bytes)}），"
                "空间不够时上锁 / 解锁会失败。"
            )
            self.banner.show()
        else:
            self.banner.hide()

    def changeEvent(self, event) -> None:  # noqa: N802
        # 切回窗口时刷新一下：别人可能用命令行或另一台电脑动过同一个文件夹
        if (
            event.type() == QEvent.ActivationChange
            and self.isActiveWindow()
            and not self._busy
            and time.monotonic() - self._last_refresh > 3
        ):
            QTimer.singleShot(0, self._refresh)
        super().changeEvent(event)

    # ---------------------------------------------------------------- 定时 / 自动上锁

    def _on_tick(self) -> None:
        idle = idle_seconds()
        for card in self.cards:
            card.update_countdown(idle)
        self._update_disk()
        if self._busy or self._auto_locking or not self.sessions:
            return
        minutes = self.store.auto_lock_minutes
        if idle is None:
            return
        if idle < minutes * 60:
            self._auto_lock_blocked = False
            return
        # 外部程序（如 CT 脱敏工具）声明正在用的保险箱先跳过：它用完、标记失效后，
        # 只要人还没回来，下一秒就会接着自动上锁
        paths = [p for p in self.sessions if not inuse.active_users(p)]
        if paths and not self._auto_lock_blocked:
            self._auto_lock_all(minutes, paths)

    def _auto_lock_all(self, minutes: int, paths: list[str] | None = None) -> None:
        self._auto_locking = True
        paths = list(self.sessions.keys()) if paths is None else paths
        try:
            self._notify("自动上锁", f"{minutes} 分钟没有操作，正在上锁。")
            for path in paths:
                vault = self.sessions.get(path)
                if vault is not None:
                    self._run_lock(vault, password=None, auto=True)
            # 只看这次尝试过的：没锁上的等有人动了键盘鼠标再试，避免每秒弹一次
            self._auto_lock_blocked = any(p in self.sessions for p in paths)
        finally:
            self._auto_locking = False
            self._refresh()

    # ---------------------------------------------------------------- 输密码（输错了直接重输）

    def _set_busy(self, busy: bool) -> None:
        # 进度框是模态的，会挡住主窗口操作；这里只设标志和光标，不禁用窗口，
        # 否则 Qt 会把子对话框一起禁用。
        self._busy = busy
        if busy:
            QApplication.setOverrideCursor(Qt.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()

    def _audit_dir(self) -> Path:
        return self.store.audit_path.parent

    def _get_credentials(
        self, path: str, action: str, *, error: str = "", note: str = ""
    ) -> tuple[str | None, bytes | None] | None:
        """弹密码框。返回 (密码, None) / (None, 恢复密钥) / None（取消）。

        点「忘记密码」：解锁和换恢复密钥走「重置密码」（以后还要用密码），
        其它操作直接用恢复密钥。
        """
        password, forgot = PasswordDialog.ask(self, path, action=action, error=error, note=note)
        if password is not None:
            return password, None
        if not forgot:
            return None
        if action in ("unlock", "regen"):
            new_pw = self.reset_password(path)
            return (new_pw, None) if new_pw else None
        key = PasswordDialog.ask_recovery(self, path)
        return (None, key) if key is not None else None

    def _with_credentials(
        self,
        path: str,
        action: str,
        run: Callable[[str | None, bytes | None], ProgressDialog | None],
        *,
        note: str = "",
    ) -> ProgressDialog | None:
        error = ""
        while True:
            cred = self._get_credentials(path, action, error=error, note=note)
            if cred is None:
                return None
            password, key = cred
            dlg = run(password, key)
            if dlg is not None and dlg.outcome == "auth_failed":
                error = "恢复密钥不对" if key is not None else "密码不对，再试一次"
                continue
            return dlg

    def _progress(self, title: str, worker: VaultOpWorker, control: OperationControl, **kw) -> ProgressDialog:
        dlg = ProgressDialog(self, title, control=control, **kw)
        dlg.start(worker)
        self._set_busy(True)
        try:
            dlg.exec()
        finally:
            self._set_busy(False)
        return dlg

    # ---------------------------------------------------------------- 新建 / 添加

    def new_vault(self, path: str | None = None) -> None:
        if self._busy:
            return
        if path is None:
            path = QFileDialog.getExistingDirectory(self, "选择要上锁的文件夹", self.store.last_vault_dir)
            if not path:
                return
            self.store.set_last_vault_dir(str(Path(path).parent))
        self.show_and_raise()
        p = Path(path).expanduser().resolve()
        if p.is_dir() and Vault(p).is_vault():
            self._add_existing(str(p))
            return
        problem = folder_problem(p)
        if problem:
            QMessageBox.warning(self, "不能用这个文件夹", problem)
            return
        wiz = CreateWizard(self, self.store, audit_dir=self._audit_dir(), initial_path=str(p))
        try:
            wiz.exec()
        finally:
            wiz.deleteLater()
        if wiz.created:
            self.store.add_vault(wiz.target)
            self._notify("已上锁", f"「{_name(wiz.target)}」已创建并上锁。")
        self._refresh()

    def _add_existing(self, path: str) -> None:
        if self.store.get(path) is not None:
            self._notify("已在列表中", f"「{_name(path)}」已经在列表里了。")
        else:
            self.store.add_vault(path)
            self._notify("已添加", f"「{_name(path)}」已加入列表。")
        self._refresh()

    # ---------------------------------------------------------------- 解锁 / 上锁

    def unlock_vault(self, path: str) -> None:
        if self._busy:
            return
        info = self._vault_info(path)
        if info.get("needs_recovery") or info["state"] in NEEDS_RECOVERY:
            self.recover_vault(path)
            return
        if info["state"] != C.LOCKED:
            self._refresh()
            return
        self._with_credentials(
            path,
            "unlock",
            lambda pw, key: self._run_unlock(Vault(path, audit_dir=self._audit_dir()), password=pw, recovery_key=key),
        )

    def _run_unlock(self, vault: Vault, *, password: str | None = None, recovery_key: bytes | None = None) -> ProgressDialog:
        path = str(vault.root)
        control = OperationControl()
        worker = VaultOpWorker(vault, "unlock", password=password, recovery_key=recovery_key, control=control, parent=self)

        def rollback() -> VaultOpWorker:
            return VaultOpWorker(vault, "recover", password=password, recovery_key=recovery_key, rollback=True, parent=self)

        dlg = self._progress(f"正在解锁「{_name(path)}」", worker, control, on_rollback=rollback)
        if dlg.outcome == "ok":
            # 解锁时得到的密钥只留在内存里，这样上锁 / 自动上锁不用再输密码；退出程序时清掉。
            self.sessions[path] = vault
            self.store.touch_unlock(path, getpass.getuser())
            self._refresh()
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        else:
            vault.close()
            self._refresh()
        return dlg

    def lock_vault(self, path: str) -> None:
        if self._busy:
            return
        vault = self.sessions.get(path)
        if vault is not None:
            if self._run_lock(vault, password=None).outcome != "auth_failed":
                return
            # 内存里的密钥没了：往下走，让用户输密码
        if self._vault_info(path)["state"] != C.UNLOCKED:
            self._refresh()
            return
        self._with_credentials(
            path,
            "lock",
            lambda pw, key: self._run_lock(Vault(path, audit_dir=self._audit_dir()), password=pw, recovery_key=key),
            note="程序重启过，需要再输一次密码。",
        )

    def _run_lock(
        self,
        vault: Vault,
        *,
        password: str | None = None,
        recovery_key: bytes | None = None,
        auto: bool = False,
    ) -> ProgressDialog:
        path = self._session_path(vault)
        control = OperationControl()
        worker = VaultOpWorker(vault, "lock", password=password, recovery_key=recovery_key, control=control, parent=self)

        def rollback() -> VaultOpWorker:
            return VaultOpWorker(vault, "recover", password=password, recovery_key=recovery_key, rollback=True, parent=self)

        dlg = self._progress(f"正在上锁「{_name(path)}」", worker, control, on_rollback=rollback)
        self._finish_lock(path, vault, dlg.outcome)
        if dlg.outcome == "ok":
            self.store.touch_lock(path)
            if auto:
                self._notify("已自动上锁", f"「{_name(path)}」已上锁。")
        self._refresh()
        return dlg

    def _session_path(self, vault: Vault) -> str:
        """会话里用的路径（按对象找，避免 /var 与 /private/var 这类写法不一致）。"""
        for key, v in self.sessions.items():
            if v is vault:
                return key
        return str(vault.root)

    def _finish_lock(self, path: str, vault: Vault, outcome: str) -> None:
        """上锁结束后处理会话：没锁上（比如文件还开着）就保留内存里的密钥，
        关掉占用的软件后再点「上锁」不用重新输密码。"""
        keep = (
            outcome not in ("ok", "auth_failed")
            and self.sessions.get(path) is vault
            and self._vault_info(path)["state"] == C.UNLOCKED
        )
        if not keep:
            self.sessions.pop(path, None)
            vault.close()

    def lock_all(self) -> None:
        if self._busy or not self.sessions:
            return
        for path in list(self.sessions.keys()):
            vault = self.sessions.get(path)
            if vault is not None:
                self._run_lock(vault, password=None)

    # ---------------------------------------------------------------- 恢复 / 校验

    def recover_vault(self, path: str) -> None:
        if self._busy:
            return
        info = self._vault_info(path)
        if info["state"] not in NEEDS_RECOVERY:
            self._refresh()
            return
        error = ""
        while True:
            password, rollback, use_key = RecoveryDialog.ask(self, path, _state_text(info["state"]), error=error)
            key = None
            if use_key:
                key = PasswordDialog.ask_recovery(self, path)
                if key is None:
                    return
            elif password is None:
                return
            vault = Vault(path, audit_dir=self._audit_dir())
            control = OperationControl()
            worker = VaultOpWorker(
                vault, "recover", password=password, recovery_key=key, rollback=rollback, control=control, parent=self
            )
            dlg = self._progress(f"正在恢复「{_name(path)}」", worker, control)
            vault.close()
            if dlg.outcome == "auth_failed":
                error = "恢复密钥不对" if key is not None else "密码不对，再试一次"
                continue
            if dlg.outcome == "ok":
                self._notify("恢复完成", f"「{_name(path)}」已恢复。")
            break
        self._refresh()

    def verify_vault(self, path: str) -> None:
        if self._busy:
            return
        if self._vault_info(path)["state"] != C.LOCKED:
            QMessageBox.information(self, "先上锁", "上锁状态下才能校验。")
            return

        def run(password: str | None, key: bytes | None) -> ProgressDialog:
            vault = Vault(path, audit_dir=self._audit_dir())
            control = OperationControl()
            worker = VaultOpWorker(vault, "verify", password=password, recovery_key=key, control=control, parent=self)
            dlg = self._progress(f"正在校验「{_name(path)}」", worker, control)
            vault.close()
            return dlg

        dlg = self._with_credentials(path, "verify", run)
        if dlg is not None and dlg.outcome == "ok" and isinstance(dlg.result_data, dict):
            result = dlg.result_data
            if result.get("ok"):
                QMessageBox.information(self, "校验通过", f"{result.get('files', 0):,} 个文件全部完好。")
            else:
                problems = result.get("problems") or []
                lines = "\n".join(f"· {p.get('path')}：{p.get('reason')}" for p in problems[:10])
                QMessageBox.warning(self, "发现问题", f"{len(problems)} 个文件有问题，请从备份恢复：\n\n{lines}")
        self._refresh()

    # ---------------------------------------------------------------- 密码 / 恢复密钥

    def _quick(self, fn: Callable[[], object]) -> object:
        """短操作（只算一次密钥，约 1 秒）：转圈光标，不开进度框。"""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            return fn()
        finally:
            QApplication.restoreOverrideCursor()

    def reset_password(self, path: str) -> str | None:
        """用恢复密钥设新密码；成功返回新密码。"""
        if self._busy:
            return None
        error = ""
        while True:
            got = ResetPasswordDialog.ask(self, path, error=error)
            if got is None:
                return None
            key, new_pw = got
            vault = Vault(path, audit_dir=self._audit_dir())
            try:
                self._quick(lambda: vault.reset_with_recovery_key(key, new_pw))
            except RecoveryKeyError:
                error = "恢复密钥不对"
                continue
            except VaultError as exc:
                QMessageBox.critical(self, "没能重置", str(exc))
                return None
            finally:
                vault.close()
            self._notify("密码已重置", "记得把新密码告诉组里同事。")
            return new_pw

    def change_password(self, path: str) -> None:
        if self._busy:
            return
        error = ""
        while True:
            got = ChangePasswordDialog.ask(self, path, error=error)
            if got is None:
                return
            old, new = got
            vault = Vault(path, audit_dir=self._audit_dir())
            try:
                self._quick(lambda: vault.change_password(old, new))
            except PasswordError:
                error = "原密码不对"
                continue
            except VaultError as exc:
                QMessageBox.critical(self, "没能修改", str(exc))
                return
            finally:
                vault.close()
            self._notify("密码已修改", "记得把新密码告诉组里同事。")
            return

    def regenerate_recovery(self, path: str) -> None:
        if self._busy:
            return
        error = ""
        while True:
            cred = self._get_credentials(path, "regen", error=error, note="旧的恢复密钥会作废。")
            if cred is None or cred[0] is None:
                return
            password = cred[0]
            vault = Vault(path, audit_dir=self._audit_dir())
            try:
                result = self._quick(lambda: vault.regenerate_recovery_key(password))
            except PasswordError:
                error = "密码不对，再试一次"
                continue
            except VaultError as exc:
                QMessageBox.critical(self, "没能生成", str(exc))
                return
            finally:
                vault.close()
            NewRecoveryKeyDialog(self, result["recovery_key"], result["recovery_fingerprint"]).exec()
            self._refresh()
            return

    # ---------------------------------------------------------------- 其他窗口

    def open_audit(self) -> None:
        if self.audit_window is None:
            self.audit_window = AuditWindow(self.store)
            self.audit_window.setAttribute(Qt.WA_DeleteOnClose, True)
            self.audit_window.destroyed.connect(lambda *_: setattr(self, "audit_window", None))
        self.audit_window.show()
        self.audit_window.raise_()
        self.audit_window.activateWindow()
        self.audit_window.refresh()

    def open_settings(self) -> None:
        dlg = SettingsDialog(self, self.store)
        if dlg.exec() == DIALOG_OK:
            self._refresh()

    # ---------------------------------------------------------------- 托盘

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(make_tray_icon() if sys_is_mac() else make_app_icon(), self)
        self.tray.setToolTip("文件保险箱")
        menu = QMenu()
        act_open = QAction("打开主窗口", self)
        act_open.triggered.connect(self.show_and_raise)
        menu.addAction(act_open)
        self.act_lock_all = QAction("全部上锁", self)
        self.act_lock_all.triggered.connect(self.lock_all)
        menu.addAction(self.act_lock_all)
        menu.addSeparator()
        act_audit = QAction("日志", self)
        act_audit.triggered.connect(self.open_audit)
        menu.addAction(act_audit)
        act_settings = QAction("设置", self)
        act_settings.triggered.connect(self.open_settings)
        menu.addAction(act_settings)
        menu.addSeparator()
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.quit_app)
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _update_tray_status(self) -> None:
        if self.tray is None:
            return
        locked = sum(1 for c in self.cards if c.info["state"] == C.LOCKED)
        unlocked = len(self.sessions)
        self.tray.setToolTip(f"文件保险箱 · {locked} 个已上锁 · {unlocked} 个已解锁")
        if hasattr(self, "act_lock_all"):
            self.act_lock_all.setEnabled(bool(self.sessions))

    def _tray_activated(self, reason) -> None:  # noqa: ANN001
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_and_raise()

    def _notify(self, title: str, message: str) -> None:
        if self.tray is not None and self.tray.isVisible():
            self.tray.showMessage(title, message, QSystemTrayIcon.Information, 4000)
        else:
            self.statusBar().showMessage(f"{title}：{message}", 4000)

    def show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    # ---------------------------------------------------------------- 关闭 / 退出

    def _lock_before_exit(self) -> list[str]:
        """退出前自动把会话里的保险箱都上锁，返回没能锁上的名字。"""
        failed: list[str] = []
        for path, vault in list(self.sessions.items()):
            control = OperationControl()
            worker = VaultOpWorker(vault, "lock", password=None, control=control, parent=self)
            dlg = self._progress(f"退出前上锁「{_name(path)}」", worker, control)
            self._finish_lock(path, vault, dlg.outcome)
            if dlg.outcome == "ok":
                self.store.touch_lock(path)
            else:
                failed.append(_name(path))
        self._refresh()
        return failed

    def _ask_quit_anyway(self, failed: list[str]) -> str:
        """自动上锁失败时问用户怎么办；返回 background / quit / cancel。"""
        has_tray = self.tray is not None and self.tray.isVisible()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("有保险箱没锁上")
        box.setText(f"没锁上：{'、'.join(failed)}")
        box.setInformativeText("可能有文件还开着。关掉相关软件后再上锁。")
        buttons: dict[str, object] = {}
        for key, text in quit_choices(has_tray):
            role = QMessageBox.DestructiveRole if key == "quit" else (
                QMessageBox.AcceptRole if key == "background" else QMessageBox.RejectRole
            )
            buttons[key] = box.addButton(text, role)
        box.setDefaultButton(buttons["background"] if has_tray else buttons["cancel"])
        box.exec()
        clicked = box.clickedButton()
        for key, btn in buttons.items():
            if clicked is btn:
                return key
        return "cancel"

    def _go_background(self) -> None:
        self.hide()
        if self.tray is not None:
            self.tray.show()
            self._notify("还在后台", "关掉占用文件的软件后，点图标 →「全部上锁」。")

    def prepare_quit(self) -> bool:
        """退出前把会话里的保险箱都锁回去。返回 True 表示可以退出（已标记 _quitting）。

        托盘「退出」和系统发来的退出请求（⌘Q、Dock 右键退出、注销、关机，见 gui/app.py 的
        QuitGuard）都走这里，保证不管怎么退都会先上锁。"""
        if self._quitting:
            return True
        if self._quit_in_progress:
            return False  # 正在上锁时又来一次退出请求：等这次走完
        self._quit_in_progress = True
        try:
            failed = self._lock_before_exit() if self.sessions else []
            if failed:
                choice = self._ask_quit_anyway(failed)
                if choice == "cancel":
                    return False
                if choice == "background":
                    self._go_background()
                    return False
        finally:
            self._quit_in_progress = False
        self._quitting = True
        for v in list(self.sessions.values()):
            v.close()
        self.sessions.clear()
        if self.tray is not None:
            self.tray.hide()
        return True

    def quit_app(self) -> None:
        if self.prepare_quit():
            QApplication.quit()

    def commit_data(self, manager) -> None:  # noqa: ANN001
        """Windows 注销 / 关机（WM_QUERYENDSESSION）：先上锁；锁不上就请求取消关机。"""
        if self._quitting or not self.sessions:
            return
        failed = self._lock_before_exit()
        if failed:
            logging.warning("注销/关机前有保险箱没锁上：%s", failed)
            try:
                manager.cancel()
            except Exception:  # noqa: BLE001
                pass

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._quitting:
            event.accept()
            return
        if self.store.minimize_to_tray and self.tray is not None and self.tray.isVisible():
            event.ignore()
            self.hide()
            if not self.store.data.get("tray_hint_shown"):
                self.store.data["tray_hint_shown"] = True
                self.store.save()
                self._notify("还在后台运行", "退出请点图标 →「退出」。")
            return
        failed = self._lock_before_exit() if self.sessions else []
        if failed:
            choice = self._ask_quit_anyway(failed)
            if choice == "cancel":
                event.ignore()
                return
            if choice == "background":
                event.ignore()
                self._go_background()
                return
        event.accept()


def sys_is_mac() -> bool:
    return sys.platform == "darwin"
