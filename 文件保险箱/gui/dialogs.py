"""密码框、密保作答、进度框、恢复、改密码 / 重置密码 / 设密保问题、设置、操作日志窗口。

弹窗统一一个样子：居中大图标 → 标题 → 输入 → 两个等宽大按钮（取消 / 主操作）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import __version__, crypto, fsutil
from core.audit import WEAK_AUDIT_NOTE, AuditLog
from core.control import OperationControl
from core.errors import RecoveryKeyError

from .store import AUTO_LOCK_CHOICES, AppStore, app_data_dir
from .style import button, hero_header, hline, label, spacer
from .widgets import AnswerForm, PasswordPair, QuestionsEditor
from .workers import VaultOpWorker

DIALOG_USE_RECOVERY = 2

# 这几种失败说明“输错了”，界面直接让用户重输，不弹报错框
AUTH_ERRORS = {"PasswordError", "RecoveryKeyError", "AnswerError"}

_ACTIONS = {
    # action: (图标, 标题模板, 主按钮)
    "unlock": ("locked", "解锁「{name}」", "解锁"),
    "lock": ("unlocked", "上锁「{name}」", "上锁"),
    "verify": ("locked", "校验「{name}」", "开始校验"),
    "recover": ("warn", "恢复「{name}」", "开始恢复"),
}


def _vault_name(vault_path: str) -> str:
    return Path(vault_path).name if vault_path else "保险箱"


def _base_layout(dlg: QDialog, width: int = 380) -> QVBoxLayout:
    dlg.setModal(True)
    dlg.setFixedWidth(width)
    root = QVBoxLayout(dlg)
    root.setContentsMargins(30, 26, 30, 24)
    root.setSpacing(12)
    return root


def _hero(root: QVBoxLayout, kind: str, title: str, subtitle: str = "", *, size: int = 72) -> tuple[QLabel, QLabel]:
    w, t, s = hero_header(kind, title, subtitle, size=size)
    root.addWidget(w)
    root.addSpacing(6)
    return t, s


def _buttons(root: QVBoxLayout, ok_text: str, on_ok, on_cancel, *, cancel_text: str = "取消") -> QPushButton:
    """两个等宽大按钮：左取消，右主操作。"""
    root.addSpacing(6)
    row = QHBoxLayout()
    row.setSpacing(10)
    cancel = button(cancel_text, kind="cta2", on_click=on_cancel)
    ok = button(ok_text, kind="cta", on_click=on_ok)
    ok.setDefault(True)
    ok.setAutoDefault(True)
    cancel.setAutoDefault(False)
    row.addWidget(cancel, 1)
    row.addWidget(ok, 1)
    root.addLayout(row)
    return ok


def _link(root: QVBoxLayout, text: str, on_click) -> QPushButton:
    link = button(text, kind="ghost", on_click=on_click)
    link.setAutoDefault(False)
    root.addWidget(link, 0, Qt.AlignHCenter)
    return link


def _error_label(text: str = "") -> QLabel:
    lb = label(text, role="danger", wrap=True)
    lb.setAlignment(Qt.AlignCenter)
    lb.setVisible(bool(text))
    return lb


def _key_input() -> QLineEdit:
    """旧版本建的保险箱还有恢复密钥，忘记密码时仍可以用它。"""
    edit = QLineEdit()
    edit.setPlaceholderText("恢复密钥  XXXX-XXXX-…")
    edit.setProperty("mono", True)
    return edit


# ================================================================ 密码 / 恢复密钥

class PasswordDialog(QDialog):
    def __init__(
        self,
        parent=None,
        vault_path: str = "",
        *,
        recovery: bool = False,
        action: str = "unlock",
        error: str = "",
        note: str = "",
    ):
        super().__init__(parent)
        self.recovery = recovery
        self.password: str | None = None
        self.recovery_key: bytes | None = None
        root = _base_layout(self)

        name = _vault_name(vault_path)
        if recovery:
            kind, title, ok_text = "key", "输入恢复密钥", "确定"
            note = note or f"「{name}」"
        else:
            kind, tpl, ok_text = _ACTIONS.get(action, _ACTIONS["unlock"])
            title = tpl.format(name=name)
        self.setWindowTitle(title)
        t, _s = _hero(root, kind, title, note)
        t.setToolTip(vault_path)

        if recovery:
            self.input = _key_input()
        else:
            # 不限制成 6 位数字：旧版本建的保险箱还在用复杂密码
            self.input = QLineEdit()
            self.input.setPlaceholderText("密码（6 位数字）")
            self.input.setEchoMode(QLineEdit.Password)
        self.input.returnPressed.connect(self._submit)
        root.addWidget(self.input)

        self.error = _error_label(error)
        root.addWidget(self.error)

        _buttons(root, ok_text, self._submit, self.reject)
        if not recovery:
            _link(root, "忘记密码？", lambda: self.done(DIALOG_USE_RECOVERY))

        self.input.setFocus()

    def _submit(self) -> None:
        text = self.input.text().strip()
        if self.recovery:
            try:
                self.recovery_key = crypto.recovery_key_parse(text)
            except RecoveryKeyError as exc:
                self._show_error(f"格式不对：{exc}")
                return
            self.accept()
            return
        if not text:
            self._show_error("请输入密码")
            return
        self.password = text
        self.accept()

    def _show_error(self, msg: str) -> None:
        self.error.setText(msg)
        self.error.show()
        self.input.selectAll()
        self.input.setFocus()

    @staticmethod
    def ask(parent, vault_path: str, *, action: str = "unlock", error: str = "", note: str = "") -> tuple[str | None, bool]:
        """返回 (密码, 是否点了「忘记密码」)。用户取消返回 (None, False)。"""
        dlg = PasswordDialog(parent, vault_path, action=action, error=error, note=note)
        code = dlg.exec()
        if code == DIALOG_USE_RECOVERY:
            return None, True
        if code == QDialog.Accepted:
            return dlg.password, False
        return None, False

    @staticmethod
    def ask_recovery(parent, vault_path: str, *, error: str = "") -> bytes | None:
        dlg = PasswordDialog(parent, vault_path, recovery=True, error=error)
        return dlg.recovery_key if dlg.exec() == QDialog.Accepted else None


class AnswersDialog(QDialog):
    """忘记密码：回答密保问题（答对任意一个即可）。"""

    def __init__(self, parent=None, vault_path: str = "", questions: list[str] | None = None, *, action: str = "unlock", error: str = ""):
        super().__init__(parent)
        self.answers: list[str] | None = None
        root = _base_layout(self, 420)
        name = _vault_name(vault_path)
        _kind, tpl, ok_text = _ACTIONS.get(action, _ACTIONS["unlock"])
        title = tpl.format(name=name)
        self.setWindowTitle(title)
        _hero(root, "key", "回答密保问题", f"{title} · 答对任意一个即可")

        self.form = AnswerForm(questions or [], self)
        self.form.changed.connect(self._update)
        root.addWidget(self.form)
        for e in self.form.edits:
            e.returnPressed.connect(self._submit)
        self.error = _error_label(error)
        root.addWidget(self.error)
        self.ok = _buttons(root, ok_text, self._submit, self.reject)
        self._update()
        self.form.focus_first()

    def _update(self) -> None:
        self.ok.setEnabled(self.form.any_filled())

    def _submit(self) -> None:
        if not self.form.any_filled():
            self.error.setText("至少回答一个问题")
            self.error.show()
            return
        self.answers = self.form.answers()
        self.accept()

    @staticmethod
    def ask(parent, vault_path: str, questions: list[str], *, action: str = "unlock", error: str = "") -> list[str] | None:
        dlg = AnswersDialog(parent, vault_path, questions, action=action, error=error)
        return dlg.answers if dlg.exec() == QDialog.Accepted else None


class ResetPasswordDialog(QDialog):
    """忘记密码：答对一个密保问题 + 设新密码。旧版本的保险箱没有密保，改用恢复密钥。"""

    def __init__(self, parent=None, vault_path: str = "", questions: list[str] | None = None, *, error: str = ""):
        super().__init__(parent)
        self.questions = list(questions or [])
        self.credential: dict | None = None
        self.new_password = ""
        root = _base_layout(self, 440)
        self.setWindowTitle("重置密码")
        if self.questions:
            sub = f"「{_vault_name(vault_path)}」· 答对任意一个密保问题即可"
        else:
            sub = f"「{_vault_name(vault_path)}」· 旧版保险箱，用恢复密钥重置"
        _hero(root, "key", "重置密码", sub)

        self.form: AnswerForm | None = None
        self.key_input: QLineEdit | None = None
        if self.questions:
            self.form = AnswerForm(self.questions, self)
            self.form.changed.connect(self._update)
            root.addWidget(self.form)
            root.addSpacing(4)
        else:
            self.key_input = _key_input()
            self.key_input.textChanged.connect(self._update)
            root.addWidget(self.key_input)
        root.addWidget(label("新密码对全组生效", role="muted"))
        self.pair = PasswordPair(self, first_placeholder="新密码（6 位数字）")
        self.pair.changed.connect(self._update)
        root.addWidget(self.pair)
        self.error = _error_label(error)
        root.addWidget(self.error)

        self.ok = _buttons(root, "重置", self._submit, self.reject)
        self._update()
        if self.form is not None:
            self.form.focus_first()
        elif self.key_input is not None:
            self.key_input.setFocus()

    def _has_proof(self) -> bool:
        if self.form is not None:
            return self.form.any_filled()
        return bool(self.key_input is not None and self.key_input.text().strip())

    def _update(self) -> None:
        self.ok.setEnabled(self._has_proof() and self.pair.is_ok())

    def _submit(self) -> None:
        if not self.ok.isEnabled():
            return
        if self.form is not None:
            self.credential = {"answers": self.form.answers()}
        else:
            try:
                self.credential = {"recovery_key": crypto.recovery_key_parse(self.key_input.text().strip())}
            except RecoveryKeyError as exc:
                self.error.setText(f"恢复密钥格式不对：{exc}")
                self.error.show()
                return
        self.new_password = self.pair.password()
        self.accept()

    @staticmethod
    def ask(parent, vault_path: str, questions: list[str], *, error: str = "") -> tuple[dict, str] | None:
        """返回 ({"answers": [...]} 或 {"recovery_key": bytes}, 新密码)；取消返回 None。"""
        dlg = ResetPasswordDialog(parent, vault_path, questions, error=error)
        if dlg.exec() == QDialog.Accepted and dlg.credential is not None:
            return dlg.credential, dlg.new_password
        return None


class NewPasswordDialog(QDialog):
    """用密保解锁之后，顺手设一个新的 6 位密码（可以跳过）。"""

    def __init__(self, parent=None, vault_path: str = ""):
        super().__init__(parent)
        self.new_password = ""
        root = _base_layout(self, 400)
        self.setWindowTitle("设置新密码")
        _hero(root, "locked", "设置新密码", f"「{_vault_name(vault_path)}」已解锁。设个新密码，下次直接用它。")
        self.pair = PasswordPair(self, first_placeholder="新密码（6 位数字）")
        self.pair.changed.connect(self._update)
        root.addWidget(self.pair)
        self.ok = _buttons(root, "设置", self._submit, self.reject, cancel_text="以后再说")
        self._update()
        self.pair.pw1.setFocus()

    def _update(self) -> None:
        self.ok.setEnabled(self.pair.is_ok())

    def _submit(self) -> None:
        if self.pair.is_ok():
            self.new_password = self.pair.password()
            self.accept()

    @staticmethod
    def ask(parent, vault_path: str) -> str | None:
        dlg = NewPasswordDialog(parent, vault_path)
        return dlg.new_password if dlg.exec() == QDialog.Accepted else None


class ChangePasswordDialog(QDialog):
    def __init__(self, parent=None, vault_path: str = "", *, error: str = ""):
        super().__init__(parent)
        self.old_password = ""
        self.new_password = ""
        root = _base_layout(self, 420)
        self.setWindowTitle("修改密码")
        _hero(root, "locked", "修改密码", f"「{_vault_name(vault_path)}」· 记得告诉组里同事")

        self.old = QLineEdit()
        self.old.setEchoMode(QLineEdit.Password)
        self.old.setPlaceholderText("原密码")
        root.addWidget(self.old)
        self.pair = PasswordPair(self, first_placeholder="新密码（6 位数字）")
        self.pair.changed.connect(self._update)
        root.addWidget(self.pair)
        self.error = _error_label(error)
        root.addWidget(self.error)

        self.ok = _buttons(root, "修改", self._submit, self.reject)
        self.old.textChanged.connect(self._update)
        self._update()
        self.old.setFocus()

    def _update(self) -> None:
        self.ok.setEnabled(bool(self.old.text()) and self.pair.is_ok())

    def _submit(self) -> None:
        if not self.ok.isEnabled():
            return
        if self.old.text() == self.pair.password():
            self.error.setText("新密码和原密码一样")
            self.error.show()
            return
        self.old_password = self.old.text()
        self.new_password = self.pair.password()
        self.accept()

    @staticmethod
    def ask(parent, vault_path: str, *, error: str = "") -> tuple[str, str] | None:
        dlg = ChangePasswordDialog(parent, vault_path, error=error)
        if dlg.exec() == QDialog.Accepted:
            return dlg.old_password, dlg.new_password
        return None


class SecurityQuestionsDialog(QDialog):
    """设置 / 修改 3 个密保问题：先输当前密码。旧版保险箱设好后，原来的恢复密钥作废。"""

    def __init__(
        self,
        parent=None,
        vault_path: str = "",
        current: list[str] | None = None,
        *,
        legacy_key: bool = False,
        error: str = "",
    ):
        super().__init__(parent)
        self.password = ""
        self.pairs: list[tuple[str, str]] = []
        root = _base_layout(self, 520)
        title = "修改密保问题" if current else "设置密保问题"
        self.setWindowTitle(title)
        if legacy_key:
            sub = "设好后原来的恢复密钥作废，忘记密码改为答对任意一个问题。"
        else:
            sub = "忘记密码时，答对其中任意一个就能解锁。旧的问题和答案会作废。"
        _hero(root, "key", title, f"「{_vault_name(vault_path)}」· {sub}", size=64)

        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        self.pw.setPlaceholderText("当前密码")
        self.pw.textChanged.connect(self._update)
        root.addWidget(self.pw)
        self.editor = QuestionsEditor(self, initial=current)
        self.editor.changed.connect(self._update)
        root.addWidget(self.editor)
        self.error = _error_label(error)
        root.addWidget(self.error)
        self.ok = _buttons(root, "保存", self._submit, self.reject)
        self._update()
        self.pw.setFocus()

    def _update(self) -> None:
        self.ok.setEnabled(bool(self.pw.text()) and self.editor.is_ok())

    def _submit(self) -> None:
        if not self.ok.isEnabled():
            return
        self.password = self.pw.text()
        self.pairs = self.editor.pairs()
        self.accept()

    @staticmethod
    def ask(
        parent, vault_path: str, current: list[str], *, legacy_key: bool = False, error: str = ""
    ) -> tuple[str, list[tuple[str, str]]] | None:
        dlg = SecurityQuestionsDialog(parent, vault_path, current, legacy_key=legacy_key, error=error)
        if dlg.exec() == QDialog.Accepted:
            return dlg.password, dlg.pairs
        return None


# ================================================================ 进度对话框

class ProgressDialog(QDialog):
    # 操作很快（小文件夹）时不闪一下窗口：超过这个时间还没完成才显示出来
    REVEAL_DELAY_MS = 350

    def __init__(
        self,
        parent,
        title: str,
        subtitle: str = "",
        *,
        control: OperationControl,
        on_rollback: Callable[[], VaultOpWorker] | None = None,
        cancellable: bool = True,
        kind: str = "",
    ):
        super().__init__(parent)
        self.setModal(True)
        self.setFixedWidth(400)
        self.setWindowTitle(title)
        self.control = control
        self.on_rollback = on_rollback
        self.outcome = "failed"  # ok / rolled_back / cancelled / failed / auth_failed
        self.error_name = ""
        self.error_msg = ""
        self._worker: VaultOpWorker | None = None
        self._rb: VaultOpWorker | None = None
        self._rolling = False
        self._cancellable = cancellable
        self.result_data = None
        self._t0 = time.monotonic()
        self._last_bytes = 0
        self._last_t = self._t0

        if not kind:
            kind = "unlocked" if "解锁" in title else "locked"
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 22)
        root.setSpacing(10)
        self.title_label, self.subtitle = _hero(root, kind, title, subtitle, size=64)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        root.addWidget(self.bar)

        stats = QHBoxLayout()
        self.percent = label("0%", role="h2")
        stats.addWidget(self.percent)
        stats.addWidget(spacer())
        self.files = label("准备中…", role="muted")
        stats.addWidget(self.files)
        root.addLayout(stats)

        info = QHBoxLayout()
        self.state_label = label("", role="muted")
        info.addWidget(self.state_label)
        info.addWidget(spacer())
        self.bytes_label = label("", role="muted")
        info.addWidget(self.bytes_label)
        root.addLayout(info)

        root.addSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.pause_btn = button("暂停", kind="cta2", on_click=self._toggle_pause)
        row.addWidget(self.pause_btn, 1)
        self.cancel_btn = button("取消", kind="cta2", on_click=self._cancel_clicked)
        self.cancel_btn.setStyleSheet("color:#e0352b;")
        row.addWidget(self.cancel_btn, 1)
        if not cancellable:
            self.pause_btn.hide()
            self.cancel_btn.hide()
        root.addLayout(row)

        self._update_pause_btn()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self.REVEAL_DELAY_MS > 0 and self.outcome == "failed" and self._worker is not None:
            self.setWindowOpacity(0.0)
            QTimer.singleShot(self.REVEAL_DELAY_MS, lambda: self.setWindowOpacity(1.0))

    # ---------------------------------------------------------------- 启动 / 信号

    def start(self, worker: VaultOpWorker) -> None:
        self._worker = worker
        worker.progressed.connect(self._on_progress)
        worker.succeeded.connect(self._on_succeeded)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_cancelled)
        self._t0 = time.monotonic()
        self._last_t = self._t0
        worker.start()

    def _on_progress(self, d: dict) -> None:
        phase = d.get("phase")
        if phase and not self.control.paused:
            self.state_label.setText(str(phase))
        done = int(d.get("done_files") or 0)
        total = int(d.get("total_files") or 0)
        pct = int(done * 100 / total) if total else 0
        self.bar.setValue(pct)
        self.percent.setText(f"{pct}%")
        if total:
            self.files.setText(f"{done:,} / {total:,} 个文件")
        done_bytes = int(d.get("done_bytes") or 0)
        total_bytes = int(d.get("total_bytes") or 0)
        now = time.monotonic()
        dt = now - self._last_t
        if dt >= 0.35 and done_bytes >= self._last_bytes:
            speed = (done_bytes - self._last_bytes) / dt
            self._last_bytes = done_bytes
            self._last_t = now
            self.bytes_label.setText(
                f"{fsutil.human_bytes(done_bytes)} / {fsutil.human_bytes(total_bytes)} · "
                f"{fsutil.human_bytes(int(speed))}/秒"
            )

    def _close(self, accepted: bool) -> None:
        # 直接结束对话框，跳过 reject() 里“操作进行中”的拦截（线程此时正在收尾）
        QDialog.done(self, QDialog.Accepted if accepted else QDialog.Rejected)

    def _on_succeeded(self, result) -> None:  # noqa: ANN001
        self.result_data = result
        self.outcome = "ok"
        self.bar.setValue(100)
        self._close(True)

    def _on_failed(self, name: str, msg: str) -> None:
        self.error_name, self.error_msg = name, msg
        if name in AUTH_ERRORS:
            self.outcome = "auth_failed"  # 调用方重新弹密码框，不弹报错
        else:
            self.outcome = "failed"
            self.setWindowOpacity(1.0)
            QMessageBox.critical(self, "没有完成", msg)
        self._close(False)

    def _on_cancelled(self) -> None:
        if self._rolling:
            return
        if self.on_rollback is None:
            self.outcome = "cancelled"
            self._close(False)
            return
        self._rolling = True
        self.pause_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.title_label.setText("正在撤销…")
        self.state_label.setText("")
        self.bar.setRange(0, 0)
        worker = self.on_rollback()
        self._rb = worker
        worker.succeeded.connect(self._rollback_ok)
        worker.failed.connect(self._rollback_failed)
        worker.start()

    def _rollback_ok(self, result=None) -> None:  # noqa: ANN001
        self.outcome = "rolled_back"
        self._close(True)

    def _rollback_failed(self, name: str, msg: str) -> None:
        self.outcome = "failed"
        QMessageBox.warning(self, "撤销没有完成", f"{msg}\n\n数据没丢。重新打开程序后点「恢复」。")
        self._close(False)

    # ---------------------------------------------------------------- 按钮

    def _toggle_pause(self) -> None:
        if self.control.paused:
            self.control.resume()
        else:
            self.control.pause()
        self._update_pause_btn()

    def _update_pause_btn(self) -> None:
        self.pause_btn.setText("继续" if self.control.paused else "暂停")
        self.state_label.setText("已暂停" if self.control.paused else "")

    def _cancel_clicked(self) -> None:
        if not self._cancellable:
            return
        if (
            QMessageBox.question(
                self,
                "取消",
                "取消后会撤销到操作前的样子，文件不会丢。确定取消？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        self.pause_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.state_label.setText("正在取消…")
        self.control.cancel()

    def _running(self) -> bool:
        return (self._worker is not None and self._worker.isRunning()) or (
            self._rb is not None and self._rb.isRunning()
        )

    def reject(self) -> None:
        if self._running():
            return  # Esc / 关闭按钮：操作中不响应，用「取消」按钮安全撤销
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._running():
            event.ignore()
            return
        super().closeEvent(event)


# ================================================================ 恢复

class RecoveryDialog(QDialog):
    def __init__(self, parent=None, vault_path: str = "", state_text: str = "", *, error: str = ""):
        super().__init__(parent)
        self.password: str | None = None
        self.rollback = False
        self.use_recovery_key = False
        root = _base_layout(self, 400)
        self.setWindowTitle("恢复保险箱")
        _hero(root, "warn", f"「{_vault_name(vault_path)}」上次没完成", "文件没丢。输入密码就能接着完成。")

        self.input = QLineEdit()
        self.input.setEchoMode(QLineEdit.Password)
        self.input.setPlaceholderText("密码")
        self.input.returnPressed.connect(self._submit)
        root.addWidget(self.input)

        self.error = _error_label(error)
        root.addWidget(self.error)

        self.radio_continue = QRadioButton("接着完成（推荐）")
        self.radio_rollback = QRadioButton("撤销，回到操作前")
        self.radio_continue.setChecked(True)
        radios = QHBoxLayout()
        radios.setSpacing(18)
        radios.addStretch(1)
        radios.addWidget(self.radio_continue)
        radios.addWidget(self.radio_rollback)
        radios.addStretch(1)
        root.addLayout(radios)

        _buttons(root, "开始恢复", self._submit, self.reject)
        _link(root, "忘记密码？", lambda: self.done(DIALOG_USE_RECOVERY))
        self.input.setFocus()

    def _submit(self) -> None:
        if not self.input.text():
            self.error.setText("请输入密码")
            self.error.show()
            return
        self.password = self.input.text()
        self.rollback = self.radio_rollback.isChecked()
        self.accept()

    @staticmethod
    def ask(parent, vault_path: str, state_text: str = "", *, error: str = "") -> tuple[str | None, bool, bool]:
        """返回 (密码, 是否回滚, 是否用恢复密钥)。取消返回 (None, False, False)。

        选「忘记密码」时回滚选项沿用对话框里当时的选择。
        """
        dlg = RecoveryDialog(parent, vault_path, state_text, error=error)
        code = dlg.exec()
        if code == DIALOG_USE_RECOVERY:
            return None, dlg.radio_rollback.isChecked(), True
        if code == QDialog.Accepted:
            return dlg.password, dlg.rollback, False
        return None, False, False


# ================================================================ 设置

def _tray_place() -> str:
    return "菜单栏" if sys.platform == "darwin" else "任务栏右下角"


def _setting_row(text: str, widget: QWidget) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(16, 10, 12, 10)
    lay.addWidget(label(text))
    lay.addWidget(spacer())
    lay.addWidget(widget)
    return w


class SettingsDialog(QDialog):
    def __init__(self, parent, store: AppStore):
        super().__init__(parent)
        self.store = store
        self.setProperty("sheet", "gray")
        root = _base_layout(self, 420)
        root.setSpacing(12)
        self.setWindowTitle("设置")
        root.addWidget(label("设置", role="h1"))

        group = QFrame()
        group.setProperty("group", True)
        gl = QVBoxLayout(group)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.setSpacing(0)

        self.minutes = QComboBox()
        for m in AUTO_LOCK_CHOICES:
            self.minutes.addItem(f"{m} 分钟", m)
        idx = self.minutes.findData(store.auto_lock_minutes)
        self.minutes.setCurrentIndex(idx if idx >= 0 else self.minutes.findData(15))
        gl.addWidget(_setting_row("无操作自动上锁", self.minutes))
        gl.addWidget(hline())

        self.tray_check = QCheckBox("")
        self.tray_check.setChecked(store.minimize_to_tray)
        self.tray_check.setCursor(Qt.PointingHandCursor)
        gl.addWidget(_setting_row(f"关闭窗口后留在{_tray_place()}", self.tray_check))
        gl.addWidget(hline())

        data_btn = button("打开")
        data_btn.setToolTip(str(app_data_dir()))
        data_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(app_data_dir()))))
        gl.addWidget(_setting_row("数据文件夹", data_btn))
        root.addWidget(group)

        ver = label(f"文件保险箱 v{__version__} · 本机加密，不联网", role="muted")
        ver.setAlignment(Qt.AlignCenter)
        root.addWidget(ver)

        _buttons(root, "保存", self._save, self.reject)

    def _save(self) -> None:
        self.store.set_auto_lock(int(self.minutes.currentData()))
        self.store.set_minimize_to_tray(self.tray_check.isChecked())
        self.accept()


# ================================================================ 操作日志

class AuditWindow(QWidget):
    def __init__(self, store: AppStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("操作日志 — 文件保险箱")
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.resize(940, 600)
        self.audit = AuditLog(store.audit_path)

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 20, 26, 16)
        root.setSpacing(12)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(label("操作日志", role="title"))
        bar.addSpacing(10)
        self.filter = QComboBox()
        self.filter.setMinimumWidth(160)
        self.filter.addItem("全部保险箱", "")
        self.filter.currentIndexChanged.connect(lambda _i: self.refresh())
        bar.addWidget(self.filter)
        bar.addWidget(spacer())
        bar.addWidget(button("刷新", on_click=self.refresh))
        bar.addWidget(button("防篡改校验", on_click=self._verify))
        bar.addWidget(button("导出 CSV", kind="primary", on_click=self._export))
        root.addLayout(bar)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["时间", "操作", "账户", "保险箱", "结果", "详情"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.setColumnWidth(0, 160)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 190)
        self.table.setColumnWidth(4, 60)
        root.addWidget(self.table, 1)

        self.count_label = label("", role="muted")
        # 「半弱审计」说明放到鼠标悬停提示里，不常驻占位置
        self.count_label.setToolTip(WEAK_AUDIT_NOTE)
        root.addWidget(self.count_label)

    def refresh(self) -> None:
        try:
            records = list(reversed(self.audit.records()))
        except Exception:  # noqa: BLE001
            records = []
        paths: list[str] = []
        for r in records:
            p = str(r.get("vault_path") or "")
            if p and p not in paths:
                paths.append(p)
        current = self.filter.currentData()
        self.filter.blockSignals(True)
        self.filter.clear()
        self.filter.addItem("全部保险箱", "")
        for p in paths:
            self.filter.addItem(Path(p).name, p)
        idx = self.filter.findData(current)
        self.filter.setCurrentIndex(idx if idx >= 0 else 0)
        self.filter.blockSignals(False)

        needle = self.filter.currentData() or ""
        rows = [r for r in records if not needle or str(r.get("vault_path") or "") == needle][:500]
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            ok = r.get("result") == "ok"
            values = [
                str(r.get("ts", "")).replace("T", " ")[:19],
                _event_name(str(r.get("event", ""))),
                str(r.get("actor", "")),
                Path(str(r.get("vault_path") or "")).name or "—",
                "成功" if ok else "失败",
                str(r.get("detail", "")),
            ]
            for j, v in enumerate(values):
                item = QTableWidgetItem(v)
                if j == 4 and not ok:
                    item.setForeground(Qt.darkRed)
                if j == 3:
                    item.setToolTip(str(r.get("vault_path") or ""))
                self.table.setItem(i, j, item)
        shown = f"共 {len(records)} 条" if len(rows) == len(records) else f"共 {len(records)} 条，显示最新 {len(rows)} 条"
        self.count_label.setText(f"{shown}  ⓘ")

    def _verify(self) -> None:
        ok, count, detail = self.audit.verify()
        if ok:
            QMessageBox.information(self, "校验通过", f"{count} 条记录，没有发现改动。")
        else:
            QMessageBox.warning(self, "发现问题", f"{detail}\n\n共 {count} 条记录，日志可能被改过。")

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出操作日志", str(Path.home() / "文件保险箱-操作日志.csv"), "CSV 文件 (*.csv)"
        )
        if not path:
            return
        try:
            self.audit.export_csv(path)
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))


_EVENT_NAMES = {
    "CREATE": "创建",
    "UNLOCK": "解锁",
    "UNLOCK_FAILED": "解锁失败",
    "LOCK": "上锁",
    "RECOVER": "恢复",
    "VERIFY": "完整性校验",
    "CHANGE_PASSWORD": "修改密码",
    "RESET_BY_RECOVERY": "重置密码（恢复密钥）",
    "RESET_BY_QUESTIONS": "重置密码（密保）",
    "RESET_FAILED": "重置密码失败",
    "SET_QUESTIONS": "设置密保问题",
    "RESET_PASSWORD": "重置密码",
    "REGENERATE_RECOVERY": "换恢复密钥",
}


def _event_name(event: str) -> str:
    return _EVENT_NAMES.get(event, event or "—")
