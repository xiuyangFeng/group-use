"""主窗口、托盘、自动上锁。"""

from __future__ import annotations

import getpass
import shutil
import time
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication,
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
from core.audit import AuditLog
from core.control import OperationControl
from core.errors import RecoveryNeededError, VaultError
from core.vault import Vault

from .dialogs import AuditWindow, PasswordDialog, ProgressDialog, RecoveryDialog, SettingsDialog
from .store import AppStore, idle_seconds
from .quit_flow import quit_choices
from .style import (
    AMBER,
    AMBER_SOFT,
    GREEN,
    GREEN_SOFT,
    INK2,
    MUTED,
    button,
    label,
    make_app_icon,
    make_tray_icon,
    make_vault_icon,
    spacer,
)
from .wizard import CreateWizard
from .workers import VaultOpWorker

DIALOG_OK = 1


def _state_text(state: str) -> str:
    return {
        C.LOCKED: "已上锁",
        C.UNLOCKED: "已解锁",
        C.LOCKING: "待恢复（上锁中断）",
        C.UNLOCKING: "待恢复（解锁中断）",
        C.INCONSISTENT: "异常，需要备份",
    }.get(state, state)


class VaultCard(QFrame):
    def __init__(self, main: "MainWindow", info: dict):
        super().__init__()
        self.main = main
        self.info = info
        self.setProperty("card", True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(14)

        state = info["state"]
        icon = QLabel()
        icon.setFixedSize(46, 46)
        icon.setAlignment(Qt.AlignCenter)
        icon.setPixmap(make_vault_icon(state == C.UNLOCKED))
        lay.addWidget(icon)

        meta = QVBoxLayout()
        meta.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(8)
        name = Path(info["path"]).name or info["path"]
        top.addWidget(label(name, role="h2"))

        badge = QLabel()
        if state == C.LOCKED:
            badge.setText("已上锁")
            badge.setStyleSheet(f"color:#475569;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:6px;padding:2px 7px;font-size:11px;")
        elif state == C.UNLOCKED:
            badge.setText("已解锁")
            badge.setStyleSheet(f"color:{GREEN};background:{GREEN_SOFT};border:1px solid #a7f3d0;border-radius:6px;padding:2px 7px;font-size:11px;")
        elif state in (C.LOCKING, C.UNLOCKING, C.INCONSISTENT):
            badge.setText("需要恢复")
            badge.setStyleSheet(f"color:{AMBER};background:{AMBER_SOFT};border:1px solid #fde68a;border-radius:6px;padding:2px 7px;font-size:11px;")
        elif state == "missing":
            badge.setText("找不到")
            badge.setStyleSheet("color:#b91c1c;background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:2px 7px;font-size:11px;")
        else:
            badge.setText("打不开")
            badge.setStyleSheet("color:#b91c1c;background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:2px 7px;font-size:11px;")
        top.addWidget(badge)

        self.countdown = QLabel("")
        self.countdown.setStyleSheet(f"color:{GREEN};font-weight:600;font-size:12px;")
        if state == C.UNLOCKED:
            top.addWidget(self.countdown)
        top.addWidget(spacer())
        meta.addLayout(top)

        meta.addWidget(label(info["path"], role="path"))
        self.meta_label = label(self._sub_text(), role="muted")
        meta.addWidget(self.meta_label)
        lay.addLayout(meta, 1)

        btns = QHBoxLayout()
        btns.setSpacing(6)
        if state == C.LOCKED:
            detail = button("详情", on_click=self._details)
            detail.setStyleSheet("padding:4px 10px;font-size:12px;")
            btns.addWidget(detail)
            go = button("解锁", kind="primary", on_click=self._unlock)
            btns.addWidget(go)
        elif state == C.UNLOCKED:
            open_btn = button("打开文件夹", on_click=self._open_folder)
            btns.addWidget(open_btn)
            go = button("立即上锁", kind="primary", on_click=self._lock)
            btns.addWidget(go)
        elif state in (C.LOCKING, C.UNLOCKING, C.INCONSISTENT):
            go = button("恢复", kind="primary", on_click=self._recover)
            btns.addWidget(go)
        elif state == "missing":
            rm = button("从列表移除", on_click=self._remove)
            btns.addWidget(rm)
        else:
            detail = button("详情", on_click=self._details)
            btns.addWidget(detail)
            rm = button("从列表移除", on_click=self._remove)
            btns.addWidget(rm)
        lay.addLayout(btns)

    # ---------------------------------------------------------------- 文本

    def _sub_text(self) -> str:
        info = self.info
        state = info["state"]
        if state == "missing":
            return "这个文件夹不在原来的位置（被移动或删除）。如果只是改了名，请从列表移除后重新添加。"
        if state == "error":
            return f"打不开：{info.get('error', '')}"
        files = int(info.get("file_count") or 0)
        total = int(info.get("total_bytes") or 0)
        parts = [f"{files:,} 个文件", fsutil.human_bytes(total)]
        ref = self.main.store.get(info["path"]) or {}
        if state == C.UNLOCKED:
            parts.append(f"空闲 {self.main.store.auto_lock_minutes} 分钟自动上锁")
        elif ref.get("last_unlock_at"):
            who = f"（{ref['last_unlock_by']}）" if ref.get("last_unlock_by") else ""
            parts.append(f"上次解锁 {ref['last_unlock_at']}{who}")
        if state in (C.LOCKING, C.UNLOCKING):
            parts.append("上次操作没有完成，文件没丢，点「恢复」")
        if state == C.UNLOCKED and info["path"] not in self.main.sessions:
            parts.append("⚠ 上次退出时没锁回，明文还在，请尽快上锁")
        return " · ".join(parts)

    # ---------------------------------------------------------------- 定时

    def update_countdown(self, idle: float | None) -> None:
        if self.info["state"] != C.UNLOCKED:
            return
        minutes = self.main.store.auto_lock_minutes
        if idle is None:
            self.countdown.setText(f"自动上锁 {minutes} 分钟")
            return
        remain = int(max(0, minutes * 60 - idle))
        self.countdown.setText(f"自动上锁 {remain // 60:02d}:{remain % 60:02d}")

    # ---------------------------------------------------------------- 操作

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
        name = Path(self.info["path"]).name
        if (
            QMessageBox.question(
                self.main,
                "从列表移除",
                f"只把「{name}」从列表里去掉，不会删除文件夹里的任何数据。\n确定吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            == QMessageBox.Yes
        ):
            self.main.store.remove_vault(self.info["path"])
            self.main._refresh()

    def _details(self) -> None:
        info = self.info
        lines = [
            f"状态：{_state_text(info['state'])}",
            f"文件数：{int(info.get('file_count') or 0):,}",
            f"总大小：{fsutil.human_bytes(int(info.get('total_bytes') or 0))}",
            f"恢复密钥指纹：{info.get('recovery_fingerprint') or '—'}",
            "",
            "密码忘了可以用恢复密钥重置；恢复密钥只在创建时显示一次，程序不保存。",
        ]
        box = QMessageBox(self.main)
        box.setWindowTitle("保险箱详情")
        box.setText(Path(info["path"]).name)
        box.setInformativeText("\n".join(lines))
        box.addButton("关闭", QMessageBox.RejectRole)
        finder = None
        if info["state"] != "missing":
            finder = box.addButton("在 Finder 中显示", QMessageBox.ActionRole)
        verify = None
        if info["state"] == C.LOCKED:
            verify = box.addButton("校验完整性", QMessageBox.ActionRole)
        remove = box.addButton("从列表移除", QMessageBox.DestructiveRole)
        box.exec()
        if box.clickedButton() is finder and finder is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(info["path"]))
        elif box.clickedButton() is verify and verify is not None:
            self.main.verify_vault(info["path"])
        elif box.clickedButton() is remove:
            self.main.store.remove_vault(info["path"])
            self.main._refresh()


class MainWindow(QMainWindow):
    def __init__(self, store: AppStore):
        super().__init__()
        self.store = store
        self.sessions: dict[str, Vault] = {}
        self.cards: list[VaultCard] = []
        self._busy = False
        self._quitting = False
        self._auto_locking = False
        self._last_disk_update = 0.0
        self._free_bytes = 0
        self._free_pct = 100.0
        self.audit_window: AuditWindow | None = None
        self.tray: QSystemTrayIcon | None = None

        self.setWindowTitle("文件保险箱 — 组内敏感数据")
        self.setWindowIcon(make_app_icon())
        self.resize(920, 660)
        self.setMinimumSize(780, 540)

        self._build_ui()
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
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(12)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(label("我的保险箱", role="h1"))
        bar.addWidget(spacer())
        bar.addWidget(button("审计日志", on_click=self.open_audit))
        bar.addWidget(button("设置", on_click=self.open_settings))
        bar.addWidget(button("新建保险箱", kind="primary", on_click=self.new_vault))
        root.addLayout(bar)

        self.banner = QFrame()
        self.banner.setProperty("banner", "warn")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(14, 10, 14, 10)
        self.banner_label = label("", role="muted", wrap=True)
        bl.addWidget(self.banner_label, 1)
        self.banner.hide()
        root.addWidget(self.banner)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(10)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_container)
        root.addWidget(self.scroll, 1)

        self.empty = QWidget()
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(0, 30, 0, 30)
        el.setSpacing(10)
        el.addStretch(1)
        icon = QLabel()
        icon.setPixmap(make_vault_icon(False, 64))
        icon.setAlignment(Qt.AlignCenter)
        el.addWidget(icon)
        t = label("还没有保险箱", role="h2")
        t.setAlignment(Qt.AlignCenter)
        el.addWidget(t)
        s = label("把患者影像、伦理材料、论文数据这类文件夹锁起来，\n解锁后照常双击打开，用完一键锁回。", role="muted", wrap=True)
        s.setAlignment(Qt.AlignCenter)
        el.addWidget(s)
        b = button("新建第一个保险箱", kind="primary", on_click=self.new_vault)
        el.addWidget(b, 0, Qt.AlignCenter)
        el.addStretch(1)
        root.addWidget(self.empty, 1)

        note = QFrame()
        note.setProperty("banner", "info")
        nl = QHBoxLayout(note)
        nl.setContentsMargins(14, 9, 14, 9)
        nl.addWidget(
            label(
                "安全提示：每人一个系统账户；解锁期间文件是明文，离开电脑前请上锁；"
                "上锁后的保险箱不会自动备份，请定期拷到移动硬盘。",
                role="muted",
                wrap=True,
            ),
            1,
        )
        root.addWidget(note)

        status = QHBoxLayout()
        self.status_left = label("", role="muted")
        status.addWidget(self.status_left)
        status.addWidget(spacer())
        self.status_disk = label("", role="muted")
        status.addWidget(self.status_disk)
        root.addLayout(status)

        self.setCentralWidget(central)

    # ---------------------------------------------------------------- 刷新

    def _vault_info(self, path: str) -> dict:
        if not Path(path).is_dir():
            return {"path": path, "state": "missing", "exists": False}
        try:
            info = Vault(path).status()  # type: ignore[union-attr]
            info["path"] = path
            info["exists"] = True
            return info
        except VaultError as exc:
            return {"path": path, "state": "error", "exists": True, "error": str(exc)}
        except OSError as exc:
            return {"path": path, "state": "error", "exists": True, "error": str(exc)}

    def _refresh(self) -> None:
        # 清空旧卡片
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.cards.clear()

        vaults = self.store.vaults
        for ref in vaults:
            info = self._vault_info(ref["path"])
            card = VaultCard(self, info)
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)
            self.cards.append(card)

        self.scroll.setVisible(bool(vaults))
        self.empty.setVisible(not vaults)
        self._update_status()
        self._update_disk()
        self._update_tray_status()

    def _update_status(self) -> None:
        total = len(self.store.vaults)
        locked = unlocked = needs = 0
        for c in self.cards:
            s = c.info["state"]
            if s == C.LOCKED:
                locked += 1
            elif s == C.UNLOCKED:
                unlocked += 1
            elif s in (C.LOCKING, C.UNLOCKING, C.INCONSISTENT):
                needs += 1
        parts = [f"共 {total} 个保险箱", f"{locked} 个已上锁"]
        if unlocked:
            parts.append(f"{unlocked} 个已解锁")
        if needs:
            parts.append(f"{needs} 个需要恢复")
        text = " · ".join(parts)
        leftover = [c for c in self.cards if c.info["state"] == C.UNLOCKED and c.info["path"] not in self.sessions]
        if leftover:
            text = f"⚠ {len(leftover)} 个保险箱上次没上锁，明文还在——请尽快点「立即上锁」"
            self.status_left.setStyleSheet(f"color:{AMBER};font-weight:600;")
        else:
            self.status_left.setStyleSheet(f"color:{GREEN};font-weight:600;" if unlocked else "")
        self.status_left.setText(text + ("　（解锁中的文件是明文，用完请上锁）" if unlocked and not leftover else ""))

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
                self._free_bytes, self._free_pct = 0, 0.0
        self.status_disk.setText(
            f"磁盘剩余 {fsutil.human_bytes(self._free_bytes)}（{self._free_pct:.1f}%）"
            if self._free_bytes
            else ""
        )
        if self._free_pct < 15:
            self.banner_label.setText(
                f"磁盘只剩 {self._free_pct:.1f}%（{fsutil.human_bytes(self._free_bytes)} 可用）。"
                "上锁/解锁需要临时空间（约为最大单文件的 2 倍），空间不足会拒绝开始；"
                "建议把大文件夹拆成几个小保险箱。"
            )
            self.banner.show()
        else:
            self.banner.hide()

    # ---------------------------------------------------------------- 定时 / 自动上锁

    def _on_tick(self) -> None:
        idle = idle_seconds()
        for card in self.cards:
            card.update_countdown(idle)
        self._update_disk()
        if self._busy or self._auto_locking or not self.sessions:
            return
        minutes = self.store.auto_lock_minutes
        if idle is not None and idle >= minutes * 60:
            self._auto_lock_all(minutes)

    def _auto_lock_all(self, minutes: int) -> None:
        self._auto_locking = True
        try:
            self._notify("自动上锁", f"已超过 {minutes} 分钟没有操作，正在自动上锁 {len(self.sessions)} 个保险箱。")
            for path in list(self.sessions.keys()):
                vault = self.sessions.get(path)
                if vault is None:
                    continue
                self._run_lock(vault, password=None, auto=True)
        finally:
            self._auto_locking = False
            self._refresh()

    # ---------------------------------------------------------------- 业务操作

    def _set_busy(self, busy: bool) -> None:
        # 进度框是模态的，会挡住主窗口操作；这里只设标志和光标，不禁用窗口，
        # 否则 Qt 会把子对话框一起禁用。
        self._busy = busy
        if busy:
            QApplication.setOverrideCursor(Qt.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()

    def new_vault(self, path: str | None = None) -> None:
        if self._busy:
            return
        wiz = CreateWizard(self, self.store, initial_path=path)
        try:
            code = wiz.exec()
        finally:
            wiz.deleteLater()
        if wiz.created:
            self.store.add_vault(wiz.target)
            self._refresh()
            QMessageBox.information(
                self,
                "保险箱已创建",
                f"「{Path(wiz.target).name}」已经上锁。\n\n"
                "恢复密钥只在上一步显示过一次，程序不保存。若还没抄下来，请立刻回去找；"
                "以后忘记密码时，只能靠它重置。",
            )
        else:
            self._refresh()

    def unlock_vault(self, path: str) -> None:
        if self._busy:
            return
        info = self._vault_info(path)
        if info.get("needs_recovery") or info["state"] in (C.LOCKING, C.UNLOCKING, C.INCONSISTENT):
            self.recover_vault(path)
            return
        if info["state"] != C.LOCKED:
            self._refresh()
            return
        vault = Vault(path, audit_dir=self.store.audit_path.parent)
        password, use_recovery = PasswordDialog.ask(self, path)
        if use_recovery:
            key = PasswordDialog.ask_recovery(self, path)
            if key is None:
                vault.close()
                return
            self._run_unlock(vault, recovery_key=key)
        elif password is not None:
            self._run_unlock(vault, password=password)

    def _run_unlock(self, vault: Vault, *, password: str | None = None, recovery_key: bytes | None = None) -> None:
        path = str(vault.root)
        control = OperationControl()
        worker = VaultOpWorker(
            vault, "unlock", password=password, recovery_key=recovery_key, control=control, parent=self
        )

        def rollback() -> VaultOpWorker:
            return VaultOpWorker(
                vault, "recover", password=password, recovery_key=recovery_key, rollback=True, parent=self
            )

        dlg = ProgressDialog(
            self,
            "正在解锁",
            f"「{Path(path).name}」正在解密。完成后会恢复成普通文件夹，Word/Excel/看图软件都能直接打开。",
            control=control,
            on_rollback=rollback,
        )
        dlg.start(worker)
        self._set_busy(True)
        try:
            dlg.exec()
        finally:
            self._set_busy(False)
        if dlg.outcome == "ok":
            # 保留解锁时得到的密钥（只在内存里），这样托盘/自动上锁能直接锁回去，
            # 不用再让用户输密码；退出程序时会被清掉。
            self.sessions[path] = vault
            self.store.touch_unlock(path, getpass.getuser())
            self._refresh()
            self._notify("已解锁", f"「{Path(path).name}」已解锁。用完请在卡片上点「立即上锁」。")
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        else:
            vault.close()
            self._refresh()

    def lock_vault(self, path: str) -> None:
        if self._busy:
            return
        vault = self.sessions.get(path)
        if vault is not None:
            self._run_lock(vault, password=None)
            return
        info = self._vault_info(path)
        if info["state"] != C.UNLOCKED:
            self._refresh()
            return
        QMessageBox.information(
            self,
            "需要密码",
            "程序重启后，内存里没有解锁时用的密钥，所以上锁需要再输一次密码。",
        )
        password, use_recovery = PasswordDialog.ask(self, path)
        if use_recovery:
            key = PasswordDialog.ask_recovery(self, path)
            if key is None:
                return
            v = Vault(path, audit_dir=self.store.audit_path.parent)
            self._run_lock(v, recovery_key=key)
        elif password is not None:
            v = Vault(path, audit_dir=self.store.audit_path.parent)
            self._run_lock(v, password=password)

    def _run_lock(
        self,
        vault: Vault,
        *,
        password: str | None = None,
        recovery_key: bytes | None = None,
        auto: bool = False,
    ) -> None:
        path = str(vault.root)
        control = OperationControl()
        worker = VaultOpWorker(vault, "lock", password=password, recovery_key=recovery_key, control=control, parent=self)

        def rollback() -> VaultOpWorker:
            return VaultOpWorker(
                vault, "recover", password=password, recovery_key=recovery_key, rollback=True, parent=self
            )

        dlg = ProgressDialog(
            self,
            "正在上锁",
            f"「{Path(path).name}」正在重新加密。完成后文件夹里只剩加密数据。",
            control=control,
            on_rollback=rollback,
        )
        dlg.start(worker)
        self._set_busy(True)
        try:
            dlg.exec()
        finally:
            self._set_busy(False)
        self.sessions.pop(path, None)
        if dlg.outcome == "ok":
            vault.close()
            self.store.touch_lock(path)
            self._notify("已上锁", f"「{Path(path).name}」已上锁。")
        else:
            vault.close()
        self._refresh()

    def recover_vault(self, path: str) -> None:
        if self._busy:
            return
        info = self._vault_info(path)
        if info["state"] not in (C.LOCKING, C.UNLOCKING, C.INCONSISTENT):
            self._refresh()
            return
        state_text = _state_text(info["state"])
        password, rollback, use_recovery = RecoveryDialog.ask(self, path, state_text)
        vault = Vault(path, audit_dir=self.store.audit_path.parent)
        if use_recovery:
            key = PasswordDialog.ask_recovery(self, path)
            if key is None:
                vault.close()
                return
            password, recovery_key = None, key
        elif password is None:
            vault.close()
            return
        else:
            recovery_key = None
        control = OperationControl()
        worker = VaultOpWorker(
            vault, "recover", password=password, recovery_key=recovery_key, rollback=rollback, control=control, parent=self
        )
        dlg = ProgressDialog(
            self,
            "正在恢复",
            "正在把文件恢复到一个确定的状态。断电或关掉程序也没关系，重新打开可以接着恢复。",
            control=control,
        )
        dlg.start(worker)
        self._set_busy(True)
        try:
            dlg.exec()
        finally:
            self._set_busy(False)
        vault.close()
        if dlg.outcome == "ok":
            self._notify("恢复完成", f"「{Path(path).name}」已恢复。")
        self._refresh()

    def verify_vault(self, path: str) -> None:
        if self._busy:
            return
        info = self._vault_info(path)
        if info["state"] != C.LOCKED:
            QMessageBox.information(self, "先别校验", "只有上锁状态才能做完整性校验。")
            return
        password, use_recovery = PasswordDialog.ask(self, path)
        if password is None:
            if not use_recovery:
                return
            key = PasswordDialog.ask_recovery(self, path)
            if key is None:
                return
            password, recovery_key = None, key
        else:
            recovery_key = None
        vault = Vault(path, audit_dir=self.store.audit_path.parent)
        control = OperationControl()
        worker = VaultOpWorker(vault, "verify", password=password, recovery_key=recovery_key, control=control, parent=self)
        dlg = ProgressDialog(
            self,
            "正在校验",
            "逐个解密检查密文是否完整。只读，不会改动文件。",
            control=control,
            cancellable=True,
        )
        dlg.start(worker)
        self._set_busy(True)
        try:
            dlg.exec()
        finally:
            self._set_busy(False)
        vault.close()
        if dlg.outcome == "ok":
            result = getattr(dlg, "result_data", None)
            if isinstance(result, dict):
                if result.get("ok"):
                    QMessageBox.information(
                        self, "校验通过", f"{result.get('files', 0):,} 个文件全部完整，未发现损坏或篡改。"
                    )
                else:
                    problems = result.get("problems") or []
                    lines = "\n".join(f"· {p.get('path')}：{p.get('reason')}" for p in problems[:10])
                    QMessageBox.warning(
                        self,
                        "发现问题",
                        f"发现 {len(problems)} 个问题文件，请从备份恢复：\n\n{lines}",
                    )
        self._refresh()

    def lock_all(self) -> None:
        if self._busy:
            return
        if not self.sessions:
            QMessageBox.information(self, "没有要上锁的", "当前没有用本窗口解锁的保险箱。")
            return
        for path in list(self.sessions.keys()):
            vault = self.sessions.get(path)
            if vault is not None:
                self._run_lock(vault, password=None)

    # ---------------------------------------------------------------- 其他窗口

    def open_audit(self) -> None:
        if self.audit_window is None:
            self.audit_window = AuditWindow(self.store)
            self.audit_window.setAttribute(Qt.WA_DeleteOnClose, True)
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
        self.act_lock_all = QAction("立即上锁全部", self)
        self.act_lock_all.triggered.connect(self.lock_all)
        menu.addAction(self.act_lock_all)
        menu.addSeparator()
        act_audit = QAction("审计日志", self)
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
        if self.tray is not None:
            self.tray.showMessage(title, message, QSystemTrayIcon.Information, 5000)
        else:
            self.statusBar().showMessage(f"{title}：{message}", 5000)

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
            dlg = ProgressDialog(
                self,
                "正在上锁",
                f"退出前正在把「{Path(path).name}」锁回去。完成后文件夹里只剩加密数据。",
                control=control,
            )
            dlg.start(worker)
            self._set_busy(True)
            try:
                dlg.exec()
            finally:
                self._set_busy(False)
            self.sessions.pop(path, None)
            vault.close()
            if dlg.outcome == "ok":
                self.store.touch_lock(path)
            else:
                failed.append(Path(path).name)
        self._refresh()
        return failed

    def _ask_quit_anyway(self, failed: list[str]) -> str:
        """自动上锁失败时问用户怎么办；返回 background / quit / cancel。"""
        has_tray = self.tray is not None and self.tray.isVisible()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("有保险箱没能上锁")
        box.setText(f"{len(failed)} 个保险箱没能自动上锁：{'、'.join(failed)}")
        box.setInformativeText(
            "文件可能正被其他程序占用。这时候退出，里面的文件仍然是明文。\n"
            "建议「留在后台」：关掉占用文件的软件后还能继续上锁；"
            "或者点「取消」回去处理。"
        )
        buttons: dict[str, object] = {}
        for key, label in quit_choices(has_tray):
            role = QMessageBox.DestructiveRole if key == "quit" else (
                QMessageBox.AcceptRole if key == "background" else QMessageBox.RejectRole
            )
            buttons[key] = box.addButton(label, role)
        box.setDefaultButton(buttons["background"] if has_tray else buttons["cancel"])
        box.exec()
        clicked = box.clickedButton()
        for key, btn in buttons.items():
            if clicked is btn:
                return key
        return "cancel"

    def quit_app(self) -> None:
        failed = self._lock_before_exit() if self.sessions else []
        if failed:
            choice = self._ask_quit_anyway(failed)
            if choice == "cancel":
                return
            if choice == "background":
                self.hide()
                if self.tray is not None:
                    self.tray.show()
                    self._notify("还在后台运行", "有保险箱没锁上。关掉占用文件的软件后，点托盘「立即上锁全部」。")
                return
        self._quitting = True
        for v in list(self.sessions.values()):
            v.close()
        self.sessions.clear()
        if self.tray is not None:
            self.tray.hide()
        QApplication.quit()

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
                self._notify("还在后台运行", "文件保险箱已缩到菜单栏。退出请点菜单栏图标 →「退出」。")
            return
        failed = self._lock_before_exit() if self.sessions else []
        if failed:
            choice = self._ask_quit_anyway(failed)
            if choice == "cancel":
                event.ignore()
                return
            if choice == "background":
                event.ignore()
                self.hide()
                if self.tray is not None:
                    self.tray.show()
                    self._notify("还在后台运行", "有保险箱没锁上。关掉占用文件的软件后，点托盘「立即上锁全部」。")
                return
        event.accept()


def sys_is_mac() -> bool:
    import sys

    return sys.platform == "darwin"
