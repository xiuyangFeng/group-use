"""几个界面里共用的小部件：6 位数字密码输入、密保问题编辑 / 作答、拖放区。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRegularExpression, Qt, Signal
from PySide6.QtGui import QPainter, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core import constants as C
from core import passwords
from core import questions as Q

from .style import GREEN, MUTED, RED, button, hero_header, label, repolish

# 规则的短写法（完整文案在 core.passwords，CLI 用）
_SHORT_RULES = (f"{C.PASSWORD_LEN} 位", "纯数字")


def pin_edit(placeholder: str) -> QLineEdit:
    """只能输数字、最多 6 位的密码框（新密码用；输旧密码的框不限制，兼容旧版本的复杂密码）。"""
    edit = QLineEdit()
    edit.setEchoMode(QLineEdit.Password)
    edit.setPlaceholderText(placeholder)
    edit.setMaxLength(C.PASSWORD_LEN)
    edit.setValidator(QRegularExpressionValidator(QRegularExpression(rf"\d{{0,{C.PASSWORD_LEN}}}"), edit))
    edit.setInputMethodHints(Qt.ImhDigitsOnly)
    return edit


class PasswordPair(QWidget):
    """新密码 + 再输一遍 + 实时勾选。wizard / 修改密码 / 重置密码共用。"""

    changed = Signal()

    def __init__(self, parent=None, *, first_placeholder: str = "密码"):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self.pw1 = pin_edit(first_placeholder)
        self.pw1.textChanged.connect(self._changed)
        lay.addWidget(self.pw1)

        self.pw2 = pin_edit("再输一遍")
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


class QuestionsEditor(QWidget):
    """设 3 个密保问题：每行一个下拉框（预设问题，三个不能重复）+ 答案输入。"""

    changed = Signal()

    def __init__(self, parent=None, *, initial: list[str] | None = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)

        texts = [q for q, _k in Q.PRESETS]
        chosen = list(initial or [])
        for q in texts:
            if len(chosen) >= Q.QUESTION_COUNT:
                break
            if q not in chosen:
                chosen.append(q)

        self.combos: list[QComboBox] = []
        self.answers: list[QLineEdit] = []
        for i in range(Q.QUESTION_COUNT):
            grid.addWidget(label(f"{i + 1}.", role="muted"), i, 0)
            combo = QComboBox()
            combo.addItems(texts)
            if chosen[i] not in texts:
                combo.addItem(chosen[i])
            combo.setCurrentText(chosen[i])
            combo.currentTextChanged.connect(self._question_changed)
            grid.addWidget(combo, i, 1)
            edit = QLineEdit()
            edit.textChanged.connect(self._changed)
            grid.addWidget(edit, i, 2)
            self.combos.append(combo)
            self.answers.append(edit)
        lay.addLayout(grid)

        self.hint = QLabel()
        self.hint.setProperty("role", "chip")
        self.hint.setTextFormat(Qt.RichText)
        self.hint.setWordWrap(True)
        self.hint.setMinimumHeight(18)
        lay.addWidget(self.hint)
        self._question_changed()

    def _question_changed(self, _text: str = "") -> None:
        picked = [c.currentText() for c in self.combos]
        for i, combo in enumerate(self.combos):
            others = set(picked[:i] + picked[i + 1 :])
            model = combo.model()
            for j in range(combo.count()):
                item = model.item(j) if hasattr(model, "item") else None
                if item is not None:
                    item.setEnabled(combo.itemText(j) not in others)
            self.answers[i].setPlaceholderText(Q.placeholder(combo.currentText()))
        self._changed()

    def _changed(self) -> None:
        problems = Q.questions_problems(self.pairs())
        typed = any(e.text().strip() for e in self.answers)
        if not problems:
            text, color = "✓ 3 个问题都设好了", GREEN
        elif not typed:
            text, color = "3 个都要填；忘记密码时答对任意一个即可", MUTED
        else:
            # 只提示第一处还没好的，避免一上来满屏红字
            text, color = "○ " + problems[0], MUTED if "还没填" in problems[0] else RED
        self.hint.setText(f'<span style="color:{color}">{text}</span>')
        self.changed.emit()

    def pairs(self) -> list[tuple[str, str]]:
        return [(c.currentText(), e.text()) for c, e in zip(self.combos, self.answers)]

    def is_ok(self) -> bool:
        return not Q.questions_problems(self.pairs())


class AnswerForm(QWidget):
    """忘记密码时作答：列出这个保险箱的密保问题，答对任意一个即可。"""

    changed = Signal()

    def __init__(self, questions: list[str], parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.edits: list[QLineEdit] = []
        for i, q in enumerate(questions):
            lay.addWidget(label(f"{i + 1}. {q}", role="sub"))
            edit = QLineEdit()
            edit.setPlaceholderText(Q.placeholder(q))
            edit.textChanged.connect(lambda _t: self.changed.emit())
            lay.addWidget(edit)
            self.edits.append(edit)

    def answers(self) -> list[str]:
        return [e.text() for e in self.edits]

    def any_filled(self) -> bool:
        return any(e.text().strip() for e in self.edits)

    def focus_first(self) -> None:
        if self.edits:
            self.edits[0].setFocus()


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
