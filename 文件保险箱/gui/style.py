"""配色、字体、全局样式与图标。

设计：主窗口走 macOS「系统设置」风（浅灰底 + 白色分组列表 + 胶囊按钮），
弹窗 / 向导走「大图标 + 一个大按钮」风（参考 1Password / Cryptomator）。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
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
from PySide6.QtWidgets import QFrame, QGraphicsDropShadowEffect, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

# 苹果系统配色
INK = "#1d1d1f"
INK2 = "#3a3a3c"
MUTED = "#86868b"
LINE = "#e5e5ea"
LINE2 = "#f0f0f3"
BLUE = "#0071e3"
BLUE2 = "#0060c7"
BLUE_SOFT = "#e8f0fe"
GREEN = "#1f9d55"
GREEN_DOT = "#34c759"
GREEN_SOFT = "#e3f7ea"
AMBER = "#c45d00"
AMBER_SOFT = "#fff4e0"
RED = "#e0352b"
RED_SOFT = "#ffeceb"
BG = "#f5f5f7"
WHITE = "#ffffff"
MONO = '"SF Mono", "Menlo", "Consolas", "Courier New", monospace'

_FONT_FAMILIES = ["PingFang SC", "Hiragino Sans GB", "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC"]

QSS = """
QWidget { color: #1d1d1f; font-size: 13px; }
QMainWindow, QDialog, QWizard { background: #ffffff; }
QWidget#page { background: #f5f5f7; }
QDialog[sheet="gray"] { background: #f5f5f7; }
QLabel { background: transparent; }
QLabel[role="title"] { font-size: 26px; font-weight: 700; }
QLabel[role="h1"] { font-size: 20px; font-weight: 700; }
QLabel[role="h2"] { font-size: 15px; font-weight: 600; }
QLabel[role="section"] { color: #86868b; font-size: 12px; padding-left: 14px; }
QLabel[role="muted"] { color: #86868b; font-size: 12px; }
QLabel[role="sub"] { color: #6e6e73; font-size: 13px; }
QLabel[role="mono"] { font-family: %(mono)s; font-size: 12px; color: #3a3a3c; }
QLabel[role="path"] { font-family: %(mono)s; font-size: 11.5px; color: #86868b; }
QLabel[role="danger"] { color: #e0352b; font-size: 12.5px; }
QLabel[role="ok"] { color: #1f9d55; font-size: 12px; }
QLabel[role="chip"] { font-size: 12px; }
QLabel[role="bigkey"] { font-family: %(mono)s; font-size: 18px; font-weight: 700; color: #1d1d1f;
    letter-spacing: 1px; }

/* 按钮：默认是浅灰胶囊 */
QPushButton {
    background: #e8e8ed; border: none; border-radius: 14px;
    padding: 6px 16px; font-size: 13px; font-weight: 600; color: #1d1d1f; min-height: 16px;
}
QPushButton:hover { background: #dedee3; }
QPushButton:pressed { background: #d2d2d7; }
QPushButton:disabled { color: #b0b0b5; background: #f0f0f3; }
QPushButton[kind="primary"] { background: #0071e3; color: #ffffff; }
QPushButton[kind="primary"]:hover { background: #0077ed; }
QPushButton[kind="primary"]:pressed { background: #0060c7; }
QPushButton[kind="primary"]:disabled { background: #a6ccf5; color: #ffffff; }
QPushButton[kind="cta"] { background: #0071e3; color: #ffffff; border-radius: 10px;
    padding: 10px 28px; font-size: 14px; min-height: 18px; }
QPushButton[kind="cta"]:hover { background: #0077ed; }
QPushButton[kind="cta"]:pressed { background: #0060c7; }
QPushButton[kind="cta"]:disabled { background: #a6ccf5; color: #ffffff; }
QPushButton[kind="cta2"] { background: #e8e8ed; color: #1d1d1f; border-radius: 10px;
    padding: 10px 28px; font-size: 14px; min-height: 18px; }
QPushButton[kind="cta2"]:hover { background: #dedee3; }
QPushButton[kind="danger"] { background: #ffeceb; color: #e0352b; }
QPushButton[kind="danger"]:hover { background: #ffdedc; }
QPushButton[kind="ghost"] { background: transparent; color: #0071e3; padding: 4px 6px; font-weight: 500; }
QPushButton[kind="ghost"]:hover { color: #0060c7; text-decoration: underline; }
QPushButton[kind="flat"] { background: transparent; color: #3a3a3c; font-weight: 500; }
QPushButton[kind="flat"]:hover { background: #e8e8ed; }
QPushButton[kind="icon"] { background: transparent; border-radius: 8px; padding: 5px; min-width: 20px; }
QPushButton[kind="icon"]:hover { background: #e8e8ed; }
QPushButton[kind="more"] { background: transparent; color: #86868b; border-radius: 8px;
    font-size: 15px; font-weight: 700; padding: 3px 7px; min-width: 16px; }
QPushButton[kind="more"]:hover { background: #f0f0f3; color: #1d1d1f; }
QPushButton[kind="more"]::menu-indicator, QPushButton[kind="icon"]::menu-indicator { image: none; width: 0; }

/* 输入框 */
QLineEdit, QComboBox, QSpinBox {
    background: #ffffff; border: 1px solid #d2d2d7; border-radius: 9px;
    padding: 8px 11px; font-size: 14px; selection-background-color: #b3d7ff;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 2px solid #0071e3; padding: 7px 10px; }
QLineEdit[mono="true"] { font-family: %(mono)s; letter-spacing: 0.5px; font-size: 13px; }
QComboBox { padding-right: 26px; font-size: 13px; padding-top: 5px; padding-bottom: 5px; }
QComboBox:focus { padding-right: 25px; padding-top: 4px; padding-bottom: 4px; }
QComboBox::drop-down { border: none; width: 24px; subcontrol-origin: padding; subcontrol-position: center right; }
QComboBox QAbstractItemView { background: #ffffff; border: 1px solid #e5e5ea; border-radius: 8px;
    selection-background-color: #0071e3; selection-color: #ffffff; padding: 4px; }

QProgressBar { background: #e5e5ea; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }
QProgressBar::chunk { background: #0071e3; border-radius: 3px; }

/* 主窗口的白色分组（系统设置风） */
QFrame[group="true"] { background: #ffffff; border: 1px solid #e5e5ea; border-radius: 12px; }
QFrame[sep="true"] { background: #e5e5ea; border: none; max-height: 1px; min-height: 1px; }
QFrame[card="true"] { background: #ffffff; border: 1px solid #e5e5ea; border-radius: 12px; }
QFrame[banner="warn"] { background: #fff4e0; border: 1px solid #ffd9a0; border-radius: 10px; }
QFrame[banner="info"] { background: #e8f0fe; border: 1px solid #b3d1fb; border-radius: 10px; }
QFrame[banner="ok"] { background: #e3f7ea; border: 1px solid #b4e6c5; border-radius: 10px; }
QFrame[keycard="true"] { background: #f5f5f7; border: 1px solid #e5e5ea; border-radius: 12px; }
QFrame[drop="idle"] { background: transparent; border: 2px dashed #d2d2d7; border-radius: 18px; }
QFrame[drop="active"] { background: #e8f0fe; border: 2px dashed #0071e3; border-radius: 18px; }

QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #c7c7cc; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }

QMenu { background: #ffffff; border: 1px solid #e5e5ea; border-radius: 10px; padding: 5px; }
QMenu::item { padding: 6px 22px 6px 12px; border-radius: 6px; }
QMenu::item:selected { background: #0071e3; color: #ffffff; }
QMenu::separator { height: 1px; background: #e5e5ea; margin: 4px 8px; }

QTableWidget { background: #ffffff; border: 1px solid #e5e5ea; border-radius: 12px;
    gridline-color: #f0f0f3; selection-background-color: #e8f0fe; selection-color: #1d1d1f; }
QHeaderView::section { background: #ffffff; color: #86868b; font-weight: 600; font-size: 12px;
    border: none; border-bottom: 1px solid #e5e5ea; padding: 8px 10px; }
QTableWidget::item { padding: 6px 10px; border-bottom: 1px solid #f0f0f3; }

QCheckBox, QRadioButton { font-size: 13px; spacing: 8px; }
QCheckBox::indicator { width: 18px; height: 18px; border: 1px solid #c7c7cc; border-radius: 5px; background: #ffffff; }
QCheckBox::indicator:hover { border-color: #0071e3; }
QCheckBox::indicator:checked { background: #0071e3; border-color: #0071e3; }
QToolTip { background: #1d1d1f; color: #ffffff; border: none; padding: 6px 9px; border-radius: 7px; }
""" % {"mono": MONO}


def _arrow_qss() -> str:
    """下拉框箭头 + 勾选框的勾：QSS 改了外观后系统自带的会消失，自己画。"""
    import tempfile
    from pathlib import Path

    out = ""
    try:
        tmp = Path(tempfile.gettempdir())
        arrow = tmp / "fdvault-combo-arrow.png"
        pm = QPixmap(20, 12)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(MUTED))
        pen.setWidthF(2.2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawPolyline([QPointF(4, 3), QPointF(10, 9), QPointF(16, 3)])
        p.end()
        if pm.save(str(arrow)):
            out += "QComboBox::down-arrow { image: url(%s); width: 10px; height: 6px; }\n" % arrow.as_posix()

        check = tmp / "fdvault-check.png"
        pm = QPixmap(36, 36)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor("#ffffff"))
        pen.setWidthF(4.2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawPolyline([QPointF(9, 19), QPointF(15.5, 25.5), QPointF(27, 11)])
        p.end()
        if pm.save(str(check)):
            out += "QCheckBox::indicator:checked { image: url(%s); }\n" % check.as_posix()
    except Exception:  # noqa: BLE001
        pass
    return out


def apply_theme(app) -> None:
    f = QFont()
    f.setFamilies(_FONT_FAMILIES)
    f.setPointSize(13)
    app.setFont(f)
    app.setStyleSheet(QSS + _arrow_qss())


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
        b.setStyleSheet("padding:4px 12px;font-size:12px;")
    if on_click:
        b.clicked.connect(on_click)
    b.setCursor(Qt.PointingHandCursor)
    return b


def icon_button(icon: QIcon, tooltip: str, on_click=None, *, size: int = 18) -> QPushButton:
    from PySide6.QtCore import QSize

    b = QPushButton()
    b.setProperty("kind", "icon")
    b.setIcon(icon)
    b.setIconSize(QSize(size, size))
    b.setToolTip(tooltip)
    b.setCursor(Qt.PointingHandCursor)
    if on_click:
        b.clicked.connect(on_click)
    return b


def spacer() -> QWidget:
    w = QWidget()
    w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    return w


def hline() -> QFrame:
    f = QFrame()
    f.setProperty("sep", True)
    return f


def elide(text: str, width: int = 300) -> str:
    return text if len(text) <= width else text[:max(0, width - 1)] + "…"


def short_path(path: str) -> str:
    """把用户主目录换成 ~，路径短一点好认。"""
    from pathlib import Path

    try:
        home = str(Path.home())
        if path == home or path.startswith(home + "/") or path.startswith(home + "\\"):
            return "~" + path[len(home):]
    except Exception:  # noqa: BLE001
        pass
    return path


def repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)


def file_manager_name() -> str:
    import sys

    if sys.platform == "darwin":
        return "访达"
    if sys.platform == "win32":
        return "资源管理器"
    return "文件管理器"


def soft_shadow(w: QWidget, *, blur: int = 28, dy: int = 10, color: str = "#0071e3", alpha: int = 70) -> None:
    eff = QGraphicsDropShadowEffect(w)
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    c = QColor(color)
    c.setAlpha(alpha)
    eff.setColor(c)
    w.setGraphicsEffect(eff)


# ---------------------------------------------------------------- 图标

def _draw_lock(p: QPainter, s: float, color: QColor, unlocked: bool = False, *, keyhole: QColor | None = None) -> None:
    pen = QPen(color)
    pen.setWidthF(max(1.6, s * 0.10))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    w = s * 0.40
    x = s * 0.30
    y = s * 0.15
    if unlocked:
        path = QPainterPath()
        path.moveTo(x, y + s * 0.30)
        path.lineTo(x, y + s * 0.20)
        path.arcTo(QRectF(x, y, w, s * 0.40), 180, -150)
        p.drawPath(path)
    else:
        path = QPainterPath()
        path.moveTo(x, y + s * 0.34)
        path.lineTo(x, y + s * 0.20)
        path.arcTo(QRectF(x, y, w, s * 0.40), 180, -180)
        path.lineTo(x + w, y + s * 0.34)
        p.drawPath(path)
    body = QRectF(s * 0.20, s * 0.46, s * 0.60, s * 0.40)
    p.setBrush(color)
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(body, s * 0.09, s * 0.09)
    if keyhole is not None:
        p.setPen(QPen(keyhole, max(1.4, s * 0.075), Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(s * 0.50, s * 0.58), QPointF(s * 0.50, s * 0.73))


def _draw_key(p: QPainter, s: float, color: QColor) -> None:
    pen = QPen(color, max(1.6, s * 0.09), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QRectF(s * 0.18, s * 0.30, s * 0.32, s * 0.32))
    p.drawLine(QPointF(s * 0.50, s * 0.46), QPointF(s * 0.82, s * 0.46))
    p.drawLine(QPointF(s * 0.72, s * 0.46), QPointF(s * 0.72, s * 0.60))
    p.drawLine(QPointF(s * 0.82, s * 0.46), QPointF(s * 0.82, s * 0.57))


def _draw_alert(p: QPainter, s: float, color: QColor) -> None:
    pen = QPen(color, max(1.8, s * 0.10), Qt.SolidLine, Qt.RoundCap)
    p.setPen(pen)
    p.drawLine(QPointF(s * 0.5, s * 0.26), QPointF(s * 0.5, s * 0.56))
    p.setBrush(color)
    p.setPen(Qt.NoPen)
    r = s * 0.065
    p.drawEllipse(QPointF(s * 0.5, s * 0.72), r, r)


_HERO = {
    # kind: (渐变起, 渐变止)
    "locked": ("#3b9bff", "#0060df"),
    "unlocked": ("#3fd27a", "#14a04b"),
    "warn": ("#ffb340", "#f07c00"),
    "key": ("#8e8cff", "#5b4ff0"),
}


def hero_color(kind: str) -> str:
    return _HERO.get(kind, _HERO["locked"])[1]


def make_hero_icon(kind: str = "locked", size: int = 84) -> QPixmap:
    """弹窗顶部的大图标：渐变圆角方块 + 白色图形。"""
    ratio = 2
    pm = QPixmap(size * ratio, size * ratio)
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    a, b = _HERO.get(kind, _HERO["locked"])
    grad = QLinearGradient(0, 0, size * 0.4, size)
    grad.setColorAt(0, QColor(a))
    grad.setColorAt(1, QColor(b))
    p.setBrush(grad)
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.28, size * 0.28)
    inner = size * 0.62
    p.translate((size - inner) / 2, (size - inner) / 2)
    white = QColor("#ffffff")
    if kind == "key":
        _draw_key(p, inner, white)
    elif kind == "warn":
        _draw_alert(p, inner, white)
    else:
        _draw_lock(p, inner, white, unlocked=(kind == "unlocked"), keyhole=QColor(b))
    p.end()
    return pm


def hero_label(kind: str = "locked", size: int = 84) -> QLabel:
    lb = QLabel()
    lb.setPixmap(make_hero_icon(kind, size))
    lb.setFixedSize(size, size)
    soft_shadow(lb, blur=30, dy=10, color=hero_color(kind), alpha=80)
    return lb


def hero_header(kind: str, title: str, subtitle: str = "", *, size: int = 76) -> tuple[QWidget, QLabel, QLabel]:
    """居中的「大图标 + 标题 + 一行小字」。返回 (容器, 标题, 副标题)。"""
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 6, 0, 2)
    lay.setSpacing(10)
    icon = hero_label(kind, size)
    lay.addWidget(icon, 0, Qt.AlignHCenter)
    lay.addSpacing(4)
    t = label(title, role="h1")
    t.setAlignment(Qt.AlignCenter)
    t.setWordWrap(True)
    lay.addWidget(t)
    s = label(subtitle, role="sub", wrap=True)
    s.setAlignment(Qt.AlignCenter)
    s.setVisible(bool(subtitle))
    lay.addWidget(s)
    w.icon = icon  # type: ignore[attr-defined]
    return w, t, s


def make_app_icon() -> QIcon:
    pm = QPixmap(256, 256)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    grad = QLinearGradient(0, 0, 100, 256)
    grad.setColorAt(0, QColor("#3b9bff"))
    grad.setColorAt(1, QColor("#0060df"))
    p.setBrush(grad)
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(14, 14, 228, 228), 52, 52)
    p.translate(52, 52)
    _draw_lock(p, 152, QColor("#ffffff"), keyhole=QColor("#0060df"))
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


def make_vault_icon(unlocked: bool, size: int = 40, *, kind: str | None = None) -> QPixmap:
    """列表行左边的小图标：浅色圆角底 + 彩色锁。"""
    kind = kind or ("unlocked" if unlocked else "locked")
    soft, fg = {
        "locked": (BLUE_SOFT, BLUE),
        "unlocked": (GREEN_SOFT, GREEN),
        "warn": (AMBER_SOFT, AMBER),
        "error": (RED_SOFT, RED),
    }.get(kind, (BLUE_SOFT, BLUE))
    ratio = 2
    pm = QPixmap(size * ratio, size * ratio)
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(soft))
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.26, size * 0.26)
    inner = size * 0.62
    p.translate((size - inner) / 2, (size - inner) / 2)
    if kind == "warn":
        _draw_alert(p, inner, QColor(fg))
    elif kind == "error":
        _draw_alert(p, inner, QColor(fg))
    else:
        _draw_lock(p, inner, QColor(fg), unlocked=(kind == "unlocked"))
    p.end()
    return pm


def make_tool_icon(name: str, size: int = 18, color: str = "#6e6e73") -> QIcon:
    """顶栏的小图标：log（日志）/ gear（设置）。"""
    ratio = 2
    pm = QPixmap(size * ratio * 2, size * ratio * 2)
    pm.setDevicePixelRatio(ratio * 2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color), size * 0.10, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    s = float(size)
    if name == "log":
        p.drawRoundedRect(QRectF(s * 0.18, s * 0.10, s * 0.64, s * 0.80), s * 0.10, s * 0.10)
        for i, wfrac in enumerate((0.36, 0.36, 0.24)):
            y = s * (0.34 + i * 0.17)
            p.drawLine(QPointF(s * 0.32, y), QPointF(s * (0.32 + wfrac), y))
    else:  # gear
        cx = cy = s / 2
        teeth = 8
        path = QPainterPath()
        for i in range(teeth * 2):
            ang = math.pi * 2 * i / (teeth * 2)
            r = s * (0.42 if i % 2 == 0 else 0.33)
            for da in (-0.17, 0.17):
                a = ang + da
                pt = QPointF(cx + r * math.cos(a), cy + r * math.sin(a))
                if i == 0 and da < 0:
                    path.moveTo(pt)
                else:
                    path.lineTo(pt)
        path.closeSubpath()
        p.drawPath(path)
        p.drawEllipse(QPointF(cx, cy), s * 0.12, s * 0.12)
    p.end()
    icon = QIcon(pm)
    return icon
