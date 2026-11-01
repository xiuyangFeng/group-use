"""密码框、进度框、恢复向导、设置、审计日志窗口。"""

from __future__ import annotations

import getpass
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QUrl, Qt
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
from .style import GREEN, MUTED, button, label, spacer
from .workers import VaultOpWorker

DIALOG_USE_RECOVERY = 2


# ================================================================ 密码 / 恢复密钥

class PasswordDialog(QDialog):
    def __init__(self, parent=None, vault_path: str = "", *, recovery: bool = False):
        super().__init__(parent)
        self.recovery = recovery
        self.password: str | None = None
        self.recovery_key: bytes | None = None
        self.setModal(True)
        self.setMinimumWidth(460)
        self.setWindowTitle("用恢复密钥" if recovery else "解锁保险箱")

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(12)

        name = Path(vault_path).name if vault_path else "保险箱"
        root.addWidget(label("输入恢复密钥" if recovery else f"解锁「{name}」", role="h2"))
        root.addWidget(label(f"文件夹：{vault_path}", role="path", wrap=True))
        root.addWidget(label(f"当前系统账户：{getpass.getuser()}（会记入审计日志）", role="muted"))

        self.input = QLineEdit()
        if recovery:
            self.input.setPlaceholderText("例如 ZHD6-6KW4-……（32 位，大小写和空格无所谓）")
            self.input.setProperty("mono", True)
        else:
            self.input.setPlaceholderText("组内共用密码")
            self.input.setEchoMode(QLineEdit.Password)
        self.input.returnPressed.connect(self._submit)
        root.addWidget(self.input)

        self.error = label("", role="danger")
        self.error.hide()
        root.addWidget(self.error)

        if not recovery:
            hint = label("提示：密码由组内共用，请通过安全渠道告知同事；忘记密码可用恢复密钥。", role="muted", wrap=True)
            root.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(8)
        if not recovery:
            link = button("忘密码？用恢复密钥", kind="ghost")
            link.clicked.connect(lambda: self.done(DIALOG_USE_RECOVERY))
            row.addWidget(link)
        row.addWidget(spacer())
        cancel = button("取消")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        ok = button("使用恢复密钥" if recovery else "解锁", kind="primary")
        ok.clicked.connect(self._submit)
        ok.setDefault(True)
        row.addWidget(ok)
        root.addLayout(row)

        self.input.setFocus()

    def _submit(self) -> None:
        text = self.input.text().strip()
        if self.recovery:
            try:
                self.recovery_key = crypto.recovery_key_parse(text)
            except RecoveryKeyError as exc:
                self._show_error(f"恢复密钥格式不对：{exc}")
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

    @staticmethod
    def ask(parent, vault_path: str) -> tuple[str | None, bool]:
        """返回 (密码, 是否改用恢复密钥)。用户取消返回 (None, False)。"""
        dlg = PasswordDialog(parent, vault_path)
        code = dlg.exec()
        if code == DIALOG_USE_RECOVERY:
            return None, True
        if code == QDialog.Accepted:
            return dlg.password, False
        return None, False

    @staticmethod
    def ask_recovery(parent, vault_path: str) -> bytes | None:
        dlg = PasswordDialog(parent, vault_path, recovery=True)
        return dlg.recovery_key if dlg.exec() == QDialog.Accepted else None


# ================================================================ 进度对话框

class ProgressDialog(QDialog):
    def __init__(
        self,
        parent,
        title: str,
        subtitle: str,
        *,
        control: OperationControl,
        on_rollback: Callable[[], VaultOpWorker] | None = None,
        cancellable: bool = True,
    ):
        super().__init__(parent)
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setWindowTitle(title)
        self.control = control
        self.on_rollback = on_rollback
        self.outcome = "failed"  # ok / rolled_back / cancelled / failed
        self._worker: VaultOpWorker | None = None
        self._rb: VaultOpWorker | None = None
        self._rolling = False
        self._cancellable = cancellable
        self.result_data = None
        self._t0 = time.monotonic()
        self._last_bytes = 0
        self._last_t = self._t0

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 18)
        root.setSpacing(12)
        self.title_label = label(title, role="h2")
        root.addWidget(self.title_label)
        self.subtitle = label(subtitle, role="muted", wrap=True)
        root.addWidget(self.subtitle)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        root.addWidget(self.bar)

        stats = QHBoxLayout()
        self.percent = label("0%", role="h2")
        stats.addWidget(self.percent)
        stats.addWidget(spacer())
        self.files = label("准备中…", role="muted")
        stats.addWidget(self.files)
        root.addLayout(stats)

        self.bytes_label = label("", role="muted")
        root.addWidget(self.bytes_label)

        self.state_label = label("", role="muted")
        root.addWidget(self.state_label)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(spacer())
        self.pause_btn = button("暂停")
        self.pause_btn.clicked.connect(self._toggle_pause)
        row.addWidget(self.pause_btn)
        self.cancel_btn = button("取消", kind="danger")
        self.cancel_btn.clicked.connect(self._cancel_clicked)
        row.addWidget(self.cancel_btn)
        if not cancellable:
            self.pause_btn.hide()
            self.cancel_btn.hide()
        root.addLayout(row)

        self._update_pause_btn()

    # ---------------------------------------------------------------- 启动 / 信号

    def start(self, worker: VaultOpWorker) -> None:
        self._worker = worker
        worker.progressed.connect(self._on_progress)
        worker.succeeded.connect(self._on_succeeded)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_cancelled)
        worker.finished.connect(self._on_finished)
        self._t0 = time.monotonic()
        self._last_t = self._t0
        worker.start()

    def _on_finished(self) -> None:
        pass

    def _on_progress(self, d: dict) -> None:
        phase = d.get("phase")
        if phase:
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
                f"{fsutil.human_bytes(done_bytes)} / {fsutil.human_bytes(total_bytes)}   "
                f"{fsutil.human_bytes(int(speed))}/秒"
            )

    def _on_succeeded(self, result) -> None:  # noqa: ANN001
        self.result_data = result
        self.outcome = "ok"
        self.bar.setValue(100)
        self.accept()

    def _on_failed(self, name: str, msg: str) -> None:
        self.outcome = "failed"
        QMessageBox.critical(self, "操作失败", msg)
        self.reject()

    def _on_cancelled(self) -> None:
        if self._rolling:
            return
        if self.on_rollback is None:
            self.outcome = "cancelled"
            self.reject()
            return
        self._rolling = True
        self.pause_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.title_label.setText("正在回滚…")
        self.state_label.setText("正在把文件恢复到操作前的状态，请稍候")
        self.bar.setRange(0, 0)
        worker = self.on_rollback()
        self._rb = worker
        worker.succeeded.connect(self._rollback_ok)
        worker.failed.connect(self._rollback_failed)
        worker.start()

    def _rollback_ok(self, result=None) -> None:  # noqa: ANN001
        self.outcome = "rolled_back"
        self.accept()

    def _rollback_failed(self, name: str, msg: str) -> None:
        self.outcome = "failed"
        QMessageBox.warning(
            self,
            "回滚没有完成",
            f"操作已取消，但自动回滚没有完成：{msg}\n\n"
            "数据现在是「待恢复」状态，没有丢失。请重新打开程序，在保险箱卡片上点「恢复」。",
        )
        self.reject()

    # ---------------------------------------------------------------- 按钮

    def _toggle_pause(self) -> None:
        if self.control.paused:
            self.control.resume()
        else:
            self.control.pause()
        self._update_pause_btn()

    def _update_pause_btn(self) -> None:
        self.pause_btn.setText("继续" if self.control.paused else "暂停")
        self.state_label.setText("已暂停；点「继续」恢复" if self.control.paused else "")

    def _cancel_clicked(self) -> None:
        if not self._cancellable:
            return
        if (
            QMessageBox.question(
                self,
                "取消操作",
                "取消后会自动回滚到操作前的状态，文件不会丢失。\n确定要取消吗？",
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

    def reject(self) -> None:
        if self._worker is not None and self._worker.isRunning() and self.result() == 0:
            QMessageBox.information(self, "操作进行中", "操作还没结束。可以点「取消」让它安全回滚，或等它完成。")
            return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "操作进行中", "操作还没结束。可以点「取消」让它安全回滚，或等它完成。")
            event.ignore()
            return
        super().closeEvent(event)


# ================================================================ 恢复向导

class RecoveryDialog(QDialog):
    def __init__(self, parent=None, vault_path: str = "", state_text: str = ""):
        super().__init__(parent)
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setWindowTitle("恢复保险箱")
        self.password: str | None = None
        self.rollback = False
        self.use_recovery_key = False

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 18)
        root.setSpacing(11)
        root.addWidget(label("上次操作没有完成", role="h2"))
        root.addWidget(
            label(
                "多半是断电、强制关机或程序被关掉。文件没有丢，但需要先恢复到一个确定状态。"
                "整个过程可以在断电后安全重来。",
                role="muted",
                wrap=True,
            )
        )
        root.addWidget(label(f"文件夹：{vault_path}", role="path", wrap=True))
        if state_text:
            root.addWidget(label(f"当前状态：{state_text}", role="muted"))

        root.addWidget(label("密码", role="h2"))
        self.input = QLineEdit()
        self.input.setEchoMode(QLineEdit.Password)
        self.input.setPlaceholderText("输密码；忘记密码可改用恢复密钥")
        self.input.returnPressed.connect(self._submit)
        root.addWidget(self.input)

        self.error = label("", role="danger")
        self.error.hide()
        root.addWidget(self.error)

        self.radio_continue = QRadioButton("继续完成上次的操作（推荐）")
        self.radio_rollback = QRadioButton("回滚到操作前的状态")
        self.radio_continue.setChecked(True)
        root.addWidget(self.radio_continue)
        root.addWidget(self.radio_rollback)

        row = QHBoxLayout()
        row.setSpacing(8)
        rk = button("用恢复密钥", kind="ghost")
        rk.clicked.connect(lambda: self.done(DIALOG_USE_RECOVERY))
        row.addWidget(rk)
        row.addWidget(spacer())
        cancel = button("取消")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        ok = button("开始恢复", kind="primary")
        ok.clicked.connect(self._submit)
        row.addWidget(ok)
        root.addLayout(row)

    def _submit(self) -> None:
        if self.use_recovery_key:
            self.accept()
            return
        if not self.input.text():
            self.error.setText("请输入密码")
            self.error.show()
            return
        self.password = self.input.text()
        self.rollback = self.radio_rollback.isChecked()
        self.accept()

    @staticmethod
    def ask(parent, vault_path: str, state_text: str) -> tuple[str | None, bool, bool]:
        """返回 (密码, 是否回滚, 是否用恢复密钥)。取消返回 (None, False, False)。"""
        dlg = RecoveryDialog(parent, vault_path, state_text)
        code = dlg.exec()
        if code == DIALOG_USE_RECOVERY:
            return None, False, True
        if code == QDialog.Accepted:
            return dlg.password, dlg.rollback, False
        return None, False, False


# ================================================================ 设置

class SettingsDialog(QDialog):
    def __init__(self, parent, store: AppStore):
        super().__init__(parent)
        self.store = store
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setWindowTitle("设置")

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 18)
        root.setSpacing(12)
        root.addWidget(label("设置", role="h1"))

        root.addWidget(label("自动上锁", role="h2"))
        row = QHBoxLayout()
        row.addWidget(label("没有键盘/鼠标操作"))
        self.minutes = QComboBox()
        for m in AUTO_LOCK_CHOICES:
            self.minutes.addItem(f"{m} 分钟", m)
        idx = self.minutes.findData(store.auto_lock_minutes)
        self.minutes.setCurrentIndex(idx if idx >= 0 else self.minutes.findData(15))
        row.addWidget(self.minutes)
        row.addWidget(label("后自动上锁"))
        row.addWidget(spacer())
        root.addLayout(row)
        root.addWidget(
            label(
                "自动上锁只对「已经解锁」的保险箱生效。解锁期间文件是明文，离开电脑前请先手动上锁。",
                role="muted",
                wrap=True,
            )
        )

        self.tray_check = QCheckBox("点关闭窗口时隐藏到菜单栏（不退出）")
        self.tray_check.setChecked(store.minimize_to_tray)
        root.addWidget(self.tray_check)

        root.addWidget(label("数据位置（只在本机）", role="h2"))
        audit_row = QHBoxLayout()
        audit_row.addWidget(label(f"审计日志：{store.audit_path}", role="path", wrap=True), 1)
        open_btn = button("打开所在文件夹")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(app_data_dir()))))
        audit_row.addWidget(open_btn)
        root.addLayout(audit_row)
        root.addWidget(label("程序不联网、不上传、无遥测；配置只存在本机用户目录。", role="muted"))

        root.addWidget(label("关于", role="h2"))
        root.addWidget(label(f"文件保险箱 v{__version__} · 本地加密 · 数据安全优先", role="muted"))

        row2 = QHBoxLayout()
        row2.addWidget(spacer())
        cancel = button("取消")
        cancel.clicked.connect(self.reject)
        row2.addWidget(cancel)
        save = button("保存", kind="primary")
        save.clicked.connect(self._save)
        row2.addWidget(save)
        root.addLayout(row2)

    def _save(self) -> None:
        self.store.set_auto_lock(int(self.minutes.currentData()))
        self.store.set_minimize_to_tray(self.tray_check.isChecked())
        self.accept()


# ================================================================ 审计日志

class AuditWindow(QWidget):
    def __init__(self, store: AppStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("审计日志 — 文件保险箱")
        self.resize(1020, 640)
        self.audit = AuditLog(store.audit_path)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        bar = QHBoxLayout()
        bar.addWidget(label("审计日志", role="h1"))
        bar.addWidget(spacer())
        refresh = button("刷新")
        refresh.clicked.connect(self.refresh)
        bar.addWidget(refresh)
        verify = button("校验完整性")
        verify.clicked.connect(self._verify)
        bar.addWidget(verify)
        export = button("导出 CSV")
        export.clicked.connect(self._export)
        bar.addWidget(export)
        close = button("关闭")
        close.clicked.connect(self.close)
        bar.addWidget(close)
        root.addLayout(bar)

        filter_row = QHBoxLayout()
        filter_row.addWidget(label("保险箱："))
        self.filter = QComboBox()
        self.filter.addItem("全部", "")
        self.filter.currentIndexChanged.connect(lambda _i: self.refresh())
        filter_row.addWidget(self.filter)
        filter_row.addWidget(spacer())
        self.count_label = label("", role="muted")
        filter_row.addWidget(self.count_label)
        root.addLayout(filter_row)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["时间", "操作", "账户", "保险箱", "结果", "详情"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 165)
        self.table.setColumnWidth(1, 120)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 230)
        self.table.setColumnWidth(4, 70)
        root.addWidget(self.table, 1)

        root.addWidget(label(WEAK_AUDIT_NOTE, role="muted", wrap=True))

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
        self.filter.addItem("全部", "")
        for p in paths:
            self.filter.addItem(Path(p).name, p)
        idx = self.filter.findData(current)
        self.filter.setCurrentIndex(idx if idx >= 0 else 0)
        self.filter.blockSignals(False)

        needle = self.filter.currentData() or ""
        rows = [r for r in records if not needle or str(r.get("vault_path") or "") == needle][:500]
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            values = [
                str(r.get("ts", "")).replace("T", " ")[:19],
                _event_name(str(r.get("event", ""))),
                str(r.get("actor", "")),
                Path(str(r.get("vault_path") or "")).name or "—",
                "成功" if r.get("result") == "ok" else "失败",
                str(r.get("detail", "")),
            ]
            for j, v in enumerate(values):
                item = QTableWidgetItem(v)
                if j == 4 and r.get("result") != "ok":
                    item.setForeground(Qt.darkRed)
                self.table.setItem(i, j, item)
        self.count_label.setText(f"共 {len(records)} 条记录 · 显示最新 {len(rows)} 条（只在本机）")

    def _verify(self) -> None:
        ok, count, detail = self.audit.verify()
        if ok:
            QMessageBox.information(self, "校验通过", f"审计日志哈希链完整，共 {count} 条记录。\n{detail}")
        else:
            QMessageBox.warning(self, "校验发现问题", f"{detail}\n\n共 {count} 条记录。可能有人改动过日志文件。")

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出审计日志", str(Path.home() / "文件保险箱-审计日志.csv"), "CSV 文件 (*.csv)"
        )
        if not path:
            return
        try:
            self.audit.export_csv(path)
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "已导出", f"已导出到：\n{path}\n\n文件里包含「半弱审计」说明。")


_EVENT_NAMES = {
    "CREATE": "创建保险箱",
    "UNLOCK": "解锁",
    "UNLOCK_FAILED": "解锁失败",
    "LOCK": "上锁",
    "RECOVER": "恢复",
    "VERIFY": "完整性校验",
    "CHANGE_PASSWORD": "修改密码",
    "RESET_PASSWORD": "用恢复密钥重置密码",
    "REGENERATE_RECOVERY": "重新生成恢复密钥",
}


def _event_name(event: str) -> str:
    return _EVENT_NAMES.get(event, event or "—")
