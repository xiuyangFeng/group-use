"""几个界面里共用的小部件：新密码输入、恢复密钥卡片、拖放区。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core import passwords

from .style import GREEN, MUTED, RED, button, hero_header, label, repolish

# 规则的短写法（完整文案在 core.passwords，CLI 用）
_SHORT_RULES = ("8 位以上", "数字", "大写字母", "小写字母")


class PasswordPair(QWidget):
    """新密码 + 再输一遍 + 实时勾选。wizard / 修改密码 / 重置密码共用。"""

    changed = Signal()

    def __init__(self, parent=None, *, first_placeholder: str = "密码"):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self.pw1 = QLineEdit()
        self.pw1.setEchoMode(QLineEdit.Password)
        self.pw1.setPlaceholderText(first_placeholder)
        self.pw1.textChanged.connect(self._changed)
        lay.addWidget(self.pw1)

        self.pw2 = QLineEdit()
        self.pw2.setEchoMode(QLineEdit.Password)
        self.pw2.setPlaceholderText("再输一遍")
        self.pw2.textChanged.connect(self._changed)
        lay.addWidget(self.pw2)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.rules = QLabel()
        self.rules.setProperty("role", "chip")
        self.rules.setTextFormat(Qt.RichText)
        row.addWidget(self.rules)
        row.addStretch(1)
        self.show_pw = QCheckBox("显示")
        self.show_pw.setCursor(Qt.PointingHandCursor)
        self.show_pw.toggled.connect(self._toggle_echo)
        row.addWidget(self.show_pw)
        lay.addLayout(row)

        self.hint = QLabel()
        self.hint.setProperty("role", "chip")
        self.hint.setTextFormat(Qt.RichText)
        self.hint.setMinimumHeight(18)
        lay.addWidget(self.hint)

        self._changed()

    def _toggle_echo(self, shown: bool) -> None:
        mode = QLineEdit.Normal if shown else QLineEdit.Password
        self.pw1.setEchoMode(mode)
        self.pw2.setEchoMode(mode)

    def _changed(self) -> None:
        a, b = self.pw1.text(), self.pw2.text()
        chips = []
        for short, (_full, ok) in zip(_SHORT_RULES, passwords.password_rules(a)):
            mark, color = ("✓", GREEN) if ok else ("○", MUTED)
            chips.append(f'<span style="color:{color}">{mark} {short}</span>')
        self.rules.setText("&nbsp;&nbsp;&nbsp;".join(chips))
        if not b:
            hint, color = "", MUTED
        elif a != b:
            hint, color = "✗ 两次不一致", RED
        elif passwords.password_ok(a):
            hint, color = "✓ 两次一致", GREEN
        else:
            hint, color = "✗ 还没满足要求", RED
        self.hint.setText(f'<span style="color:{color}">{hint}</span>' if hint else "")
        self.changed.emit()

    def password(self) -> str:
        return self.pw1.text()

    def is_ok(self) -> bool:
        a = self.pw1.text()
        return passwords.password_ok(a) and a == self.pw2.text()

    def clear(self) -> None:
        self.pw1.clear()
        self.pw2.clear()


class RecoveryKeyCard(QFrame):
    """大号显示恢复密钥（两行，每行 4 组）+ 指纹。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("keycard", True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 14)
        lay.setSpacing(8)
        self.key_label = label("", role="bigkey")
        self.key_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.key_label.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.key_label)
        self.fp_label = label("", role="muted")
        self.fp_label.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.fp_label)

    def set_key(self, key: str, fingerprint: str) -> None:
        groups = [g for g in key.split("-") if g]
        if len(groups) == 8:
            key = "-".join(groups[:4]) + "\n" + "-".join(groups[4:])
        self.key_label.setText(key)
        self.fp_label.setText(f"指纹 {fingerprint}" if fingerprint else "")


class DropZone(QFrame):
    """虚线框：把文件夹拖进来，或点按钮选。"""

    clicked = Signal()

    def __init__(self, title: str, button_text: str, parent=None, *, subtitle: str = ""):
        super().__init__(parent)
        self.setProperty("drop", "idle")
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 36, 24, 36)
        lay.setSpacing(0)
        lay.addStretch(1)
        head, _t, _s = hero_header("locked", title, subtitle, size=88)
        lay.addWidget(head)
        lay.addSpacing(18)
        self.button = button(button_text, kind="cta", on_click=self.clicked.emit)
        lay.addWidget(self.button, 0, Qt.AlignCenter)
        lay.addStretch(1)

    def set_active(self, active: bool) -> None:
        self.setProperty("drop", "active" if active else "idle")
        repolish(self)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def dropped_folder(mime) -> str | None:
    """从拖放数据里取第一个文件夹路径。"""
    if mime is None or not mime.hasUrls():
        return None
    for url in mime.urls():
        path = url.toLocalFile()
        if path and Path(path).is_dir():
            return str(Path(path).resolve())
    return None


class ElidedLabel(QLabel):
    """一行文字，放不下时中间用 … 省略（长路径不会把卡片撑宽）。"""

    def __init__(self, text: str = "", parent=None, *, mode=Qt.ElideMiddle):
        super().__init__(text, parent)
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(40)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setPen(self.palette().color(self.foregroundRole()))
        p.setFont(self.font())
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), self._mode, rect.width())
        p.drawText(rect, int(self.alignment()) | int(Qt.AlignVCenter), text)
        p.end()
