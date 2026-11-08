"""新建保险箱向导：选文件夹 → 设 6 位数字密码 → 设 3 个密保问题 → 首次上锁。

从主窗口进来时文件夹已经选好（按钮先弹选择框 / 拖进来 / 右键菜单），
这时直接从「设密码」开始，少一步。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from core import fsutil
from core.control import OperationControl
from core.errors import VaultError
from core.vault import Vault

from .store import AppStore
from .style import GREEN, MUTED, RED, button, hero_header, label
from .widgets import PasswordPair, QuestionsEditor
from .workers import VaultOpWorker


def folder_problem(path: str | Path) -> str | None:
    """这个文件夹能不能拿来新建保险箱；能就返回 None，不能返回一句原因。"""
    p = Path(path).expanduser()
    if not str(path).strip() or not p.is_dir():
        return "不是一个文件夹"
    if p.name.startswith(".") or p.resolve() == Path.home().resolve():
        return "请选一个专门的资料文件夹，不要选隐藏文件夹或整个用户目录"
    if (p / ".vault").exists():
        return "这个文件夹已经是保险箱"
    return None


class FolderPage(QWizardPage):
    def __init__(self, wiz: "CreateWizard"):
        super().__init__()
        self.wiz = wiz
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 8, 24, 4)
        lay.setSpacing(12)
        head, _t, _s = hero_header("locked", "选择要上锁的文件夹")
        lay.addWidget(head)
        lay.addSpacing(6)

        row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("文件夹路径")
        self.path_edit.textChanged.connect(self._on_changed)
        row.addWidget(self.path_edit, 1)
        row.addWidget(button("选择…", on_click=self._pick))
        lay.addLayout(row)

        self.hint = label("", role="muted", wrap=True)
        lay.addWidget(self.hint)
        lay.addStretch(1)

    def _pick(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择要上锁的文件夹", self.wiz.store.last_vault_dir)
        if d:
            self.path_edit.setText(d)
            self.wiz.store.set_last_vault_dir(d)

    def _path(self) -> Path:
        return Path(self.path_edit.text().strip()).expanduser()

    def isComplete(self) -> bool:  # noqa: N802
        return bool(self.path_edit.text().strip()) and folder_problem(self._path()) is None

    def validatePage(self) -> bool:  # noqa: N802
        problem = folder_problem(self._path())
        if problem:
            QMessageBox.warning(self, "不能用这个文件夹", problem)
            return False
        self.wiz.target = str(self._path().resolve())
        return True

    def _on_changed(self) -> None:
        self.completeChanged.emit()
        if not self.path_edit.text().strip():
            self.hint.setText("")
            return
        problem = folder_problem(self._path())
        if problem:
            self.hint.setText(problem)
            self.hint.setStyleSheet(f"color:{RED};")
        else:
            self.hint.setText(f"将加密「{self._path().name}」及其中所有文件")
            self.hint.setStyleSheet(f"color:{MUTED};")


class PasswordPage(QWizardPage):
    def __init__(self, wiz: "CreateWizard"):
        super().__init__()
        self.wiz = wiz
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 8, 24, 4)
        lay.setSpacing(12)
        head, _t, self.folder_label = hero_header("locked", "设置 6 位数字密码", " ")
        lay.addWidget(head)
        lay.addSpacing(4)

        self.pair = PasswordPair(self, first_placeholder="密码（6 位数字）")
        self.pair.changed.connect(self.completeChanged.emit)
        lay.addWidget(self.pair)
        # 兼容旧代码 / 自检脚本直接访问
        self.pw1, self.pw2 = self.pair.pw1, self.pair.pw2
        self.rules, self.hint, self.show_pw = self.pair.rules, self.pair.hint, self.pair.show_pw

        lay.addStretch(1)
        note = label("首次上锁会改写文件夹里的全部文件，重要数据请先备份。", role="muted", wrap=True)
        note.setAlignment(Qt.AlignCenter)
        lay.addWidget(note)

    def initializePage(self) -> None:  # noqa: N802
        name = Path(self.wiz.target).name if self.wiz.target else ""
        self.folder_label.setText(f"「{name}」" if name else " ")
        self.pw1.setFocus()

    def _ok(self) -> bool:
        return self.pair.is_ok()

    def isComplete(self) -> bool:  # noqa: N802
        return self._ok()

    def nextId(self) -> int:  # noqa: N802
        # Qt 会随时调用 nextId 来更新按钮状态，这里不能做耗时操作或弹窗。
        return self.wiz.PAGE_QUESTIONS

    def validatePage(self) -> bool:  # noqa: N802
        # 不满足条件时只返回 False（下一步按钮本来就是灰的），不弹窗：
        # Qt 在程序化切页时也会调 validatePage，弹窗会阻塞自动化测试。
        return self._ok()


class QuestionsPage(QWizardPage):
    """设 3 个密保问题。点「开始上锁」才真正生成密钥、开始加密，之后不能返回。"""

    def __init__(self, wiz: "CreateWizard"):
        super().__init__()
        self.wiz = wiz
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 8, 24, 4)
        lay.setSpacing(10)
        head, _t, _s = hero_header(
            "key", "设置 3 个密保问题", "忘记密码时，答对其中任意一个就能解锁并重设密码。", size=64
        )
        lay.addWidget(head)
        self.editor = QuestionsEditor(self)
        self.editor.changed.connect(self.completeChanged.emit)
        lay.addWidget(self.editor)
        lay.addStretch(1)
        note = label("答案请如实填写并记住；不区分大小写，空格和标点会被忽略。", role="muted", wrap=True)
        note.setAlignment(Qt.AlignCenter)
        lay.addWidget(note)

    def initializePage(self) -> None:  # noqa: N802
        self.editor.answers[0].setFocus()

    def isComplete(self) -> bool:  # noqa: N802
        return self.editor.is_ok()

    def nextId(self) -> int:  # noqa: N802
        return self.wiz.PAGE_PROGRESS

    def validatePage(self) -> bool:  # noqa: N802
        if not self.editor.is_ok():
            return False
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            self.wiz.prepare(self.wiz.page_password.pw1.text(), self.editor.pairs())
        except VaultError as exc:
            QMessageBox.critical(self, "无法创建保险箱", str(exc))
            return False
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "无法创建保险箱", f"{type(exc).__name__}: {exc}")
            return False
        finally:
            QApplication.restoreOverrideCursor()
        return True


class ProgressPage(QWizardPage):
    def __init__(self, wiz: "CreateWizard"):
        super().__init__()
        self.wiz = wiz
        self.started = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 8, 24, 4)
        lay.setSpacing(10)
        self.head, self.head_title, _s = hero_header("locked", "正在上锁")
        lay.addWidget(self.head)
        lay.addSpacing(8)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        lay.addWidget(self.bar)
        row = QHBoxLayout()
        self.percent = label("0%", role="h2")
        row.addWidget(self.percent)
        row.addStretch(1)
        self.status = label("准备中…", role="muted")
        row.addWidget(self.status)
        lay.addLayout(row)
        self.detail = label("", role="muted")
        lay.addWidget(self.detail)
        lay.addStretch(1)
        self.warn = label("请保持程序打开，完成前不要关机。", role="muted", wrap=True)
        self.warn.setAlignment(Qt.AlignCenter)
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
        self.detail.setText(f"{fsutil.human_bytes(done_bytes)} / {fsutil.human_bytes(total_bytes)}")

    def finish_ok(self) -> None:
        self.bar.setValue(100)
        self.percent.setText("100%")
        self.head_title.setText("完成，已上锁")
        self.status.setText("✓ 已上锁")
        self.status.setStyleSheet(f"color:{GREEN};font-weight:600;")
        self.warn.setText("")

    def fail(self, msg: str) -> None:
        self.status.setText("没有完成")
        self.status.setStyleSheet(f"color:{RED};font-weight:600;")
        self.detail.setText(msg)
        self.detail.setWordWrap(True)
        self.warn.setText("数据没丢。关掉向导后，在主窗口点「恢复」。")


class CreateWizard(QWizard):
    PAGE_FOLDER = 0
    PAGE_PASSWORD = 1
    PAGE_QUESTIONS = 2
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
        self.control: OperationControl | None = None
        self.worker: VaultOpWorker | None = None

        self.setWindowTitle("新建保险箱")
        self.setWizardStyle(QWizard.ClassicStyle)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        self.setOption(QWizard.HaveHelpButton, False)
        self.resize(560, 540)
        self.setButtonText(QWizard.NextButton, "下一步")
        self.setButtonText(QWizard.BackButton, "上一步")
        self.setButtonText(QWizard.CancelButton, "取消")
        self.setButtonText(QWizard.FinishButton, "完成")
        self.setButtonText(QWizard.CommitButton, "开始上锁")

        self.page_folder = FolderPage(self)
        self.page_password = PasswordPage(self)
        self.page_questions = QuestionsPage(self)
        self.page_progress = ProgressPage(self)
        # 设好密保这一步一旦点下去就开始加密，不能再返回
        self.page_questions.setCommitPage(True)
        self.setPage(self.PAGE_FOLDER, self.page_folder)
        self.setPage(self.PAGE_PASSWORD, self.page_password)
        self.setPage(self.PAGE_QUESTIONS, self.page_questions)
        self.setPage(self.PAGE_PROGRESS, self.page_progress)
        if initial_path:
            self.page_folder.path_edit.setText(str(initial_path))
            if folder_problem(initial_path) is None:
                # 文件夹已经选好：直接从设密码开始
                self.target = str(Path(initial_path).expanduser().resolve())
                self.setStartId(self.PAGE_PASSWORD)

        for which in (QWizard.NextButton, QWizard.CommitButton, QWizard.FinishButton):
            b = self.button(which)
            if b is not None:
                b.setProperty("kind", "primary")
                b.style().unpolish(b)
                b.style().polish(b)
        self.currentIdChanged.connect(self._on_page_changed)

    # ---------------------------------------------------------------- 准备

    def prepare(self, password: str, questions: list[tuple[str, str]]) -> None:
        if self.prepared is not None:
            self._abort_prepared()
        self.vault = Vault(self.target, audit_dir=self.audit_dir)
        self.prepared = self.vault.prepare_new(password, questions)
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

    def _busy_box(self) -> None:
        QMessageBox.information(self, "正在上锁", "请等它完成，大约几秒到几分钟。")

    def reject(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self._busy_box()
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
            self._busy_box()
            event.ignore()
            return
        super().closeEvent(event)
