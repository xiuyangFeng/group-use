"""新建保险箱向导：选文件夹 → 设密码 → 抄恢复密钥 → 首次上锁。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from core.control import OperationControl
from core.errors import VaultError
from core.vault import Vault

from .store import AppStore
from .style import BLUE, GREEN, MUTED, RED, button, label, spacer
from .workers import VaultOpWorker


class FolderPage(QWizardPage):
    def __init__(self, wiz: "CreateWizard"):
        super().__init__()
        self.wiz = wiz
        self.setTitle("选择要上锁的文件夹")
        self.setSubTitle("上锁后，这个文件夹里只剩加密数据，文件名也会加密；解锁后恢复原样。")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(12)

        row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("点右边按钮选择要上锁的文件夹")
        self.path_edit.textChanged.connect(self._on_changed)
        row.addWidget(self.path_edit, 1)
        pick = button("选择文件夹…")
        pick.clicked.connect(self._pick)
        row.addWidget(pick)
        lay.addLayout(row)

        self.hint = label("", role="muted", wrap=True)
        lay.addWidget(self.hint)

        lay.addStretch(1)
        note = QFrame()
        note.setProperty("banner", "info")
        nl = QVBoxLayout(note)
        nl.setContentsMargins(12, 10, 12, 10)
        nl.addWidget(label("第一次上锁会重写文件夹里的每个文件，请先备份。", role="h2"))
        nl.addWidget(
            label(
                "上锁时间取决于文件总量（实测约 200 MB/秒，10 GB 约 1 分钟）。"
                "上锁过程中断电也不会丢数据，下次打开会让你恢复。",
                role="muted",
                wrap=True,
            )
        )
        lay.addWidget(note)

    def _pick(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择要上锁的文件夹", self.wiz.store.last_vault_dir)
        if d:
            self.path_edit.setText(d)
            self.wiz.store.set_last_vault_dir(d)

    def _path(self) -> Path:
        return Path(self.path_edit.text().strip()).expanduser()

    def isComplete(self) -> bool:  # noqa: N802
        p = self._path()
        if not self.path_edit.text().strip() or not p.is_dir():
            return False
        if p.name.startswith(".") or p == Path.home():
            return False
        return not (p / ".vault").exists()

    def validatePage(self) -> bool:  # noqa: N802
        p = self._path()
        if not p.is_dir():
            QMessageBox.warning(self, "路径不对", "请选择一个存在的文件夹。")
            return False
        if p.name.startswith(".") or p == Path.home():
            QMessageBox.warning(self, "不能选这里", "请不要直接选隐藏文件夹或整个用户主目录；请选一个专门的资料文件夹。")
            return False
        if (p / ".vault").exists():
            QMessageBox.warning(self, "已经是保险箱", "这个文件夹里已经有 .vault，不能重复创建。若是上次没创建完，请用「恢复」。")
            return False
        self.wiz.target = str(p.resolve())
        return True

    def _on_changed(self) -> None:  # noqa: F811
        self.completeChanged.emit()
        text = self.path_edit.text().strip()
        if not text:
            self.hint.setText("")
            return
        p = self._path()
        if not p.is_dir():
            self.hint.setText("这个路径不是文件夹")
            self.hint.setStyleSheet(f"color:{RED};")
        elif (p / ".vault").exists():
            self.hint.setText("这个文件夹已经是保险箱（或上次创建没完成），换个文件夹吧")
            self.hint.setStyleSheet(f"color:{RED};")
        else:
            self.hint.setText(f"将把「{p.name}」整个文件夹（含子文件夹）原地加密。")
            self.hint.setStyleSheet(f"color:{MUTED};")


class PasswordPage(QWizardPage):
    def __init__(self, wiz: "CreateWizard"):
        super().__init__()
        self.wiz = wiz
        self.setTitle("设置组内共用密码")
        self.setSubTitle("一个保险箱一把密码。密码决定能不能打开，忘了只能用恢复密钥。")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(10)

        lay.addWidget(label("密码（至少 12 位）"))
        self.pw1 = QLineEdit()
        self.pw1.setEchoMode(QLineEdit.Password)
        self.pw1.setPlaceholderText("建议用一句只有组内知道的长口令")
        self.pw1.textChanged.connect(self._changed)
        lay.addWidget(self.pw1)
        lay.addWidget(label("再输一遍"))
        self.pw2 = QLineEdit()
        self.pw2.setEchoMode(QLineEdit.Password)
        self.pw2.textChanged.connect(self._changed)
        lay.addWidget(self.pw2)

        self.hint = label("", role="muted", wrap=True)
        lay.addWidget(self.hint)

        warn = QFrame()
        warn.setProperty("banner", "warn")
        wl = QVBoxLayout(warn)
        wl.setContentsMargins(12, 10, 12, 10)
        wl.addWidget(label("几个提醒", role="h2"))
        wl.addWidget(
            label(
                "1. 密码由全组共用，请用微信/口头等安全渠道告诉同事，别写在文件夹里。\n"
                "2. 密码强度只影响「有人拿走硬盘后猜密码」的难度。\n"
                "3. 千万别忘了恢复密钥——下一页显示，只显示这一次。",
                role="muted",
                wrap=True,
            )
        )
        lay.addWidget(warn)
        lay.addStretch(1)

    def _changed(self) -> None:
        self.completeChanged.emit()

    def _ok(self) -> bool:
        a, b = self.pw1.text(), self.pw2.text()
        return len(a) >= 12 and a == b

    def isComplete(self) -> bool:  # noqa: N802
        return self._ok()

    def nextId(self) -> int:  # noqa: N802
        # 注意：Qt 会随时调用 nextId 来更新按钮状态，这里不能做耗时操作或弹窗。
        return self.wiz.PAGE_RECOVERY

    def validatePage(self) -> bool:  # noqa: N802
        # 不满足条件时只返回 False（Next 按钮本来也就是灰的），不要弹窗：
        # Qt 在程序化切页时也会调用 validatePage，弹窗会阻塞自动化测试。
        if not self._ok():
            return False
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            self.wiz.prepare(self.pw1.text())
        except VaultError as exc:
            QMessageBox.critical(self, "无法创建保险箱", str(exc))
            return False
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "无法创建保险箱", f"{type(exc).__name__}: {exc}")
            return False
        finally:
            QApplication.restoreOverrideCursor()
        return True


class RecoveryPage(QWizardPage):
    def __init__(self, wiz: "CreateWizard"):
        super().__init__()
        self.wiz = wiz
        self.setTitle("抄下恢复密钥（唯一一把）")
        self.setSubTitle("密码忘了、换人交接时用它重置密码。它只显示这一次，程序不保存。")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(10)

        box = QFrame()
        box.setProperty("card", True)
        box.setStyleSheet("QFrame[card=\"true\"]{background:#fffbeb;border:1px solid #fde68a;border-radius:12px;}")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(16, 14, 16, 14)
        bl.addWidget(label("恢复密钥", role="muted"))
        self.key_label = label("", role="bigkey", wrap=True)
        bl.addWidget(self.key_label)
        self.fp_label = label("", role="muted")
        bl.addWidget(self.fp_label)
        lay.addWidget(box)

        lay.addWidget(
            label(
                "建议：抄在纸上或打印出来，放进带锁的抽屉/文件柜；不要截图发微信，"
                "也不要存在这台电脑里（等于把钥匙和锁放在一起）。丢了它 + 忘了密码 = 数据永久打不开。",
                role="muted",
                wrap=True,
            )
        )
        self.check = QCheckBox("我已把恢复密钥抄好，并放到安全的地方")
        self.check.stateChanged.connect(lambda _s: self.completeChanged.emit())
        lay.addWidget(self.check)
        lay.addStretch(1)

    def initializePage(self) -> None:  # noqa: N802
        prepared = self.wiz.prepared or {}
        self.key_label.setText(prepared.get("recovery_key", ""))
        self.fp_label.setText(f"指纹：{prepared.get('recovery_fingerprint', '')}（可在保险箱详情里核对）")
        self.check.setChecked(False)

    def isComplete(self) -> bool:  # noqa: N802
        return self.check.isChecked()

    def nextId(self) -> int:  # noqa: N802
        return self.wiz.PAGE_PROGRESS


class ProgressPage(QWizardPage):
    def __init__(self, wiz: "CreateWizard"):
        super().__init__()
        self.wiz = wiz
        self.started = False
        self.setTitle("正在首次上锁…")
        self.setSubTitle("上锁完成后，这个文件夹里就只剩加密数据；请等进度走完再关闭。")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(12)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        lay.addWidget(self.bar)
        self.percent = label("0%", role="h2")
        lay.addWidget(self.percent)
        self.status = label("准备中…", role="muted")
        lay.addWidget(self.status)
        self.detail = label("", role="muted")
        lay.addWidget(self.detail)
        lay.addStretch(1)
        self.warn = label("正在加密，请不要关闭程序或关机。断电也没关系，下次打开会让你恢复。", role="muted", wrap=True)
        lay.addWidget(self.warn)

    def initializePage(self) -> None:  # noqa: N802
        if not self.started:
            self.started = True
            QTimer.singleShot(0, lambda: self.wiz.begin_initial_lock(self))

    def isComplete(self) -> bool:  # noqa: N802
        return self.wiz.created

    def update_progress(self, d: dict) -> None:
        done = int(d.get("done_files") or 0)
        total = int(d.get("total_files") or 0)
        pct = int(done * 100 / total) if total else 0
        self.bar.setValue(pct)
        self.percent.setText(f"{pct}%")
        if total:
            self.status.setText(f"{done:,} / {total:,} 个文件")
        done_bytes = int(d.get("done_bytes") or 0)
        total_bytes = int(d.get("total_bytes") or 0)
        from core import fsutil

        self.detail.setText(f"{fsutil.human_bytes(done_bytes)} / {fsutil.human_bytes(total_bytes)}")

    def finish_ok(self) -> None:
        self.bar.setValue(100)
        self.percent.setText("100%")
        self.status.setText("创建完成，保险箱已上锁。")
        self.status.setStyleSheet(f"color:{GREEN};font-weight:600;")
        self.warn.setText("重要：恢复密钥只在上一页显示过。如果没抄，现在点「取消」已经晚了——它已经不在程序里。")

    def fail(self, msg: str) -> None:
        self.status.setText(f"创建失败：{msg}")
        self.status.setStyleSheet(f"color:{RED};")
        self.status.setWordWrap(True)
        self.warn.setText("文件夹现在处于「待恢复」状态，数据没丢。请关闭向导，在主窗口点「恢复」。".replace("点「恢复」", "点卡片上的「恢复」"))


class CreateWizard(QWizard):
    PAGE_FOLDER = 0
    PAGE_PASSWORD = 1
    PAGE_RECOVERY = 2
    PAGE_PROGRESS = 3

    def __init__(self, parent=None, store: AppStore | None = None, audit_dir=None, initial_path: str | None = None):
        super().__init__(parent)
        self.store = store or AppStore()
        self.audit_dir = audit_dir
        self.target = ""
        self.vault: Vault | None = None
        self.prepared: dict | None = None
        self.prepared_password = ""
        self.created = False
        self.recovery_key = ""
        self.control: OperationControl | None = None
        self.worker: VaultOpWorker | None = None

        self.setWindowTitle("新建保险箱")
        self.setWizardStyle(QWizard.ModernStyle)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        self.setOption(QWizard.HaveHelpButton, False)
        self.resize(680, 540)
        self.setButtonText(QWizard.NextButton, "下一步")
        self.setButtonText(QWizard.BackButton, "上一步")
        self.setButtonText(QWizard.CancelButton, "取消")
        self.setButtonText(QWizard.FinishButton, "完成")

        self.page_folder = FolderPage(self)
        self.page_password = PasswordPage(self)
        self.page_recovery = RecoveryPage(self)
        self.page_progress = ProgressPage(self)
        self.setPage(self.PAGE_FOLDER, self.page_folder)
        self.setPage(self.PAGE_PASSWORD, self.page_password)
        self.setPage(self.PAGE_RECOVERY, self.page_recovery)
        self.setPage(self.PAGE_PROGRESS, self.page_progress)
        if initial_path:
            self.page_folder.path_edit.setText(str(initial_path))

        self.currentIdChanged.connect(self._on_page_changed)

    # ---------------------------------------------------------------- 准备

    def prepare(self, password: str) -> None:
        if self.prepared is not None:
            self._abort_prepared()
        self.vault = Vault(self.target, audit_dir=self.audit_dir)
        self.prepared = self.vault.prepare_new(password)
        self.prepared_password = password

    def _abort_prepared(self) -> None:
        if self.vault is not None:
            try:
                self.vault.abort_create()
            except Exception:  # noqa: BLE001
                pass
            self.vault.close()
        self.vault = None
        self.prepared = None
        self.prepared_password = ""

    # ---------------------------------------------------------------- 首次上锁

    def begin_initial_lock(self, page: ProgressPage) -> None:
        if self.vault is None or self.prepared is None:
            return
        self.control = OperationControl()
        self.worker = VaultOpWorker(self.vault, "initial_lock", control=self.control, parent=self)
        self.worker.progressed.connect(page.update_progress)
        self.worker.succeeded.connect(lambda _r: self._created(page))
        self.worker.failed.connect(lambda n, m: self._failed(page, m))
        self._set_buttons_locked(True)
        self.worker.start()

    def _created(self, page: ProgressPage) -> None:
        self.created = True
        self.recovery_key = (self.prepared or {}).get("recovery_key", "")
        page.finish_ok()
        self._set_buttons_locked(False)
        self.button(QWizard.CancelButton).hide()
        self.button(QWizard.BackButton).hide()
        self.button(QWizard.FinishButton).setEnabled(True)

    def _failed(self, page: ProgressPage, msg: str) -> None:
        page.fail(msg)
        self._set_buttons_locked(False)
        self.button(QWizard.CancelButton).setEnabled(True)
        self.button(QWizard.FinishButton).setEnabled(False)
        QMessageBox.critical(self, "首次上锁失败", msg)

    def _set_buttons_locked(self, locked: bool) -> None:
        for b in (QWizard.BackButton, QWizard.NextButton, QWizard.CancelButton, QWizard.FinishButton):
            btn = self.button(b)
            if btn is not None:
                btn.setEnabled(not locked)
        if not locked:
            self.button(QWizard.NextButton).setEnabled(False)
            self.button(QWizard.BackButton).setEnabled(False)

    def _on_page_changed(self, page_id: int) -> None:
        if page_id == self.PAGE_PROGRESS:
            self.button(QWizard.NextButton).hide()

    # ---------------------------------------------------------------- 关闭

    def reject(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "正在首次上锁", "加密正在进行，不能关闭。断电也不会丢数据，请等它完成。")
            return
        if self.prepared is not None and not self.created:
            # 还没开始首次上锁：恢复原样（文件夹里全是明文，删掉 .vault 即可）
            self._abort_prepared()
        super().reject()

    def accept(self) -> None:
        if not self.created:
            return
        self._set_buttons_locked(False)
        super().accept()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "正在首次上锁", "加密正在进行，不能关闭。断电也不会丢数据，请等它完成。")
            event.ignore()
            return
        super().closeEvent(event)
