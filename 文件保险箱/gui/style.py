"""配色、字体、全局样式与图标（与 docs/界面草图.html 一致）。"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget

# 与草图同一套颜色
INK = "#0f172a"
INK2 = "#334155"
MUTED = "#64748b"
LINE = "#e2e8f0"
LINE2 = "#eef2f7"
BLUE = "#2563eb"
BLUE2 = "#1d4ed8"
BLUE_SOFT = "#eff6ff"
GREEN = "#059669"
GREEN_SOFT = "#ecfdf5"
AMBER = "#b45309"
AMBER_SOFT = "#fffbeb"
RED = "#dc2626"
RED_SOFT = "#fef2f2"
BG = "#eef2f7"
WHITE = "#ffffff"
MONO = '"SF Mono", "Menlo", "Consolas", "Courier New", monospace'

_FONT_FAMILIES = ["PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC"]

QSS = """
QWidget { color: #0f172a; font-size: 13px; }
QMainWindow, QDialog { background: #ffffff; }
QWidget#page { background: #eef2f7; }
QLabel { background: transparent; }
QLabel[role="h1"] { font-size: 19px; font-weight: 700; }
QLabel[role="h2"] { font-size: 15px; font-weight: 600; }
QLabel[role="muted"] { color: #64748b; font-size: 12px; }
QLabel[role="mono"] { font-family: %(mono)s; font-size: 12px; color: #334155; }
QLabel[role="path"] { font-family: %(mono)s; font-size: 11.5px; color: #64748b; }
QLabel[role="danger"] { color: #b91c1c; font-size: 12px; }
QLabel[role="ok"] { color: #047857; font-size: 12px; }
QLabel[role="bigkey"] { font-family: %(mono)s; font-size: 17px; font-weight: 700; color: #78350f;
    letter-spacing: 1px; }
QPushButton {
    background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px;
    padding: 6px 14px; font-size: 13px; color: #0f172a;
}
QPushButton:hover { background: #f8fafc; }
QPushButton:pressed { background: #f1f5f9; }
QPushButton:disabled { color: #94a3b8; background: #f8fafc; border-color: #eef2f7; }
QPushButton[kind="primary"] { background: #2563eb; border-color: #2563eb; color: #ffffff; font-weight: 600; }
QPushButton[kind="primary"]:hover { background: #1d4ed8; border-color: #1d4ed8; }
QPushButton[kind="primary"]:disabled { background: #93c5fd; border-color: #93c5fd; color: #eff6ff; }
QPushButton[kind="ghost"] { background: transparent; border-color: transparent; color: #2563eb; padding: 4px 8px; }
QPushButton[kind="ghost"]:hover { background: #eff6ff; }
QPushButton[kind="danger"] { background: #ffffff; border-color: #fecaca; color: #b91c1c; }
QPushButton[kind="danger"]:hover { background: #fef2f2; }
QPushButton[kind="sm"] { padding: 4px 10px; font-size: 12px; }
QLineEdit, QComboBox, QSpinBox {
    background: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px;
    padding: 7px 10px; font-size: 13px; selection-background-color: #bfdbfe;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #2563eb; }
QLineEdit[mono="true"] { font-family: %(mono)s; letter-spacing: 0.5px; }
QProgressBar { background: #e2e8f0; border: none; border-radius: 5px; height: 10px; text-align: center; }
QProgressBar::chunk { background: #2563eb; border-radius: 5px; }
QFrame[card="true"] {
    background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px;
}
QFrame[banner="warn"] {
    background: #fffbeb; border: 1px solid #fde68a; border-radius: 10px;
}
QFrame[banner="info"] {
    background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 10px;
}
QFrame[banner="ok"] {
    background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 10px;
}
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #cbd5e1; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QMenu { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 6px; }
QMenu::item { padding: 6px 22px 6px 14px; border-radius: 6px; }
QMenu::item:selected { background: #eff6ff; color: #1d4ed8; }
QMenu::separator { height: 1px; background: #eef2f7; margin: 5px 8px; }
QTableWidget { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px;
    gridline-color: #eef2f7; selection-background-color: #eff6ff; selection-color: #0f172a; }
QHeaderView::section { background: #f8fafc; color: #475569; font-weight: 600; font-size: 12px;
    border: none; border-bottom: 1px solid #e2e8f0; padding: 7px 8px; }
QTableWidget::item { padding: 5px 8px; }
QCheckBox { font-size: 13px; }
QCheckBox::indicator { width: 17px; height: 17px; border: 1px solid #cbd5e1; border-radius: 4px;
    background: #ffffff; }
QCheckBox::indicator:checked { background: #2563eb; border-color: #2563eb; }
QRadioButton { font-size: 13px; }
QToolTip { background: #0f172a; color: #ffffff; border: none; padding: 5px 8px; border-radius: 6px; }
""" % {"mono": MONO}


def apply_theme(app) -> None:
    f = QFont()
    f.setFamilies(_FONT_FAMILIES)
    f.setPointSize(13)
    app.setFont(f)
    app.setStyleSheet(QSS)


# ---------------------------------------------------------------- 小工具

def label(text: str = "", *, role: str | None = None, wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if role:
        lb.setProperty("role", role)
    lb.setWordWrap(wrap)
    return lb


def button(text: str, *, kind: str = "default", on_click=None, small: bool = False) -> QPushButton:
    b = QPushButton(text)
    if kind and kind != "default":
        b.setProperty("kind", kind)
    if small:
        b.setProperty("kind", (kind if kind != "default" else "default") + " sm" if False else kind)
        b.setProperty("small", True)
        b.setStyleSheet("padding:4px 10px;font-size:12px;")
    if on_click:
        b.clicked.connect(on_click)
    b.setCursor(Qt.PointingHandCursor)
    return b


def spacer() -> QWidget:
    w = QWidget()
    w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    return w


def hline() -> QWidget:
    w = QWidget()
    w.setFixedHeight(1)
    w.setStyleSheet("background:#eef2f7;")
    return w


def elide(text: str, width: int = 300) -> str:
    return text if len(text) <= width else text[:max(0, width - 1)] + "…"


# ---------------------------------------------------------------- 图标

def _draw_lock(p: QPainter, s: float, color: QColor, unlocked: bool = False) -> None:
    pen = QPen(color)
    pen.setWidthF(max(1.6, s * 0.10))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    # 锁梁
    w = s * 0.42
    x = s * 0.29
    y = s * 0.16
    if unlocked:
        path = QPainterPath()
        path.moveTo(x + w * 0.98, y + s * 0.24)
        path.arcTo(QRectF(x, y, w, s * 0.44), 10, 160)
        p.drawPath(path)
    else:
        p.drawArc(QRectF(x, y, w, s * 0.44), 0, 180 * 16)
    # 锁体
    body = QRectF(s * 0.20, s * 0.48, s * 0.60, s * 0.38)
    p.setBrush(color)
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(body, s * 0.08, s * 0.08)
    if not unlocked:
        p.setPen(QPen(QColor("#ffffff"), max(1.2, s * 0.07)))
        p.drawLine(int(s * 0.50), int(s * 0.57), int(s * 0.50), int(s * 0.72))


def make_app_icon() -> QIcon:
    pm = QPixmap(256, 256)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    grad = QLinearGradient(0, 0, 256, 256)
    grad.setColorAt(0, QColor("#3b82f6"))
    grad.setColorAt(1, QColor("#1d4ed8"))
    p.setBrush(grad)
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(6, 6, 244, 244), 58, 58)
    p.translate(36, 36)
    p.scale(184 / 100.0, 184 / 100.0)
    _draw_lock(p, 100, QColor("#ffffff"))
    p.end()
    return QIcon(pm)


def make_tray_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 22, 32, 44):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        _draw_lock(p, size, QColor("#000000"))
        p.end()
        icon.addPixmap(pm)
    if hasattr(icon, "setIsMask"):
        icon.setIsMask(True)  # macOS 菜单栏模板图标：随深浅色自动反色
    return icon


def make_vault_icon(unlocked: bool, size: int = 40) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(GREEN_SOFT if unlocked else BLUE_SOFT))
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.28, size * 0.28)
    _draw_lock(p, size * 0.86, QColor(GREEN if unlocked else BLUE), unlocked=unlocked)
    p.end()
    return pm
