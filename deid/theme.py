"""界面配色和字体（只用 tkinter 自带的 ttk，不需要额外安装）。"""

import re
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# 颜色
BG = "#F3F5F8"        # 窗口底色
CARD = "#FFFFFF"      # 卡片
BORDER = "#DDE2EA"
TEXT = "#1F2937"
MUTED = "#6B7280"
PRIMARY = "#1E6FD9"
PRIMARY_DARK = "#1659B3"
HEADER = "#0F3D66"
HEADER_SUB = "#B9CCE0"
DANGER = "#C2410C"
OK = "#15803D"
WARN = "#B45309"
LOG_BG = "#FBFCFD"
DISABLED_BG = "#E5E7EB"
DISABLED_FG = "#9CA3AF"

if sys.platform == "win32":
    UI_FONT, MONO_FONT = "Microsoft YaHei UI", "Consolas"
elif sys.platform == "darwin":
    UI_FONT, MONO_FONT = "PingFang SC", "Menlo"
else:
    UI_FONT, MONO_FONT = "Noto Sans CJK SC", "DejaVu Sans Mono"

# Windows 用磅（系统按 DPI 缩放）；Mac / Linux 用像素（负数），避免 Tk 的 DPI 换算让字忽大忽小
_PX = sys.platform != "win32"
BASE = 13 if _PX else 10


def _size(n):
    return -n if _PX else n


def font(size=0, bold=False):
    f = (UI_FONT, _size(BASE + size))
    return f + ("bold",) if bold else f


def mono():
    return (MONO_FONT, _size(BASE - 1 if _PX else BASE))


def apply(root):
    """设置全局样式。所有窗口（包括处理记录窗口）共用。"""
    if UI_FONT not in tkfont.families(root):  # 系统没有这个字体时用默认字体，只调字号
        globals()["UI_FONT"] = tkfont.nametofont("TkDefaultFont").actual("family")
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        tkfont.nametofont(name).configure(family=UI_FONT, size=_size(BASE))
    root.configure(bg=BG)
    root.option_add("*TCombobox*Listbox.font", font())

    s = ttk.Style(root)
    s.theme_use("clam")  # 唯一能完全自定义颜色、且 Windows / Mac 外观一致的内置主题
    s.configure(".", background=BG, foreground=TEXT, font=font(), bordercolor=BORDER,
                lightcolor=BG, darkcolor=BG, troughcolor=BORDER, focuscolor=PRIMARY,
                selectbackground=PRIMARY, selectforeground="white")

    # 底色 / 卡片
    s.configure("TFrame", background=BG)
    s.configure("Card.TFrame", background=CARD)
    s.configure("Header.TFrame", background=HEADER)
    s.configure("TLabel", background=BG)
    s.configure("Card.TLabel", background=CARD)
    s.configure("CardTitle.TLabel", background=CARD, font=font(1, bold=True))
    s.configure("Hint.TLabel", background=CARD, foreground=MUTED, font=font(-1))
    s.configure("BgHint.TLabel", background=BG, foreground=MUTED, font=font(-1))
    s.configure("Title.TLabel", background=HEADER, foreground="white", font=font(6, bold=True))
    s.configure("Subtitle.TLabel", background=HEADER, foreground=HEADER_SUB, font=font(-1))
    s.configure("Status.TLabel", background=CARD, foreground=TEXT)
    s.configure("Percent.TLabel", background=CARD, foreground=PRIMARY, font=font(4, bold=True))

    # 按钮
    s.configure("TButton", background=CARD, foreground=TEXT, bordercolor="#C9D1DC", lightcolor=CARD,
                darkcolor=CARD, padding=(12, 4))
    s.map("TButton",
          background=[("disabled", BG), ("pressed", "#E3E8EF"), ("active", "#EEF2F7")],
          lightcolor=[("disabled", BG), ("pressed", "#E3E8EF"), ("active", "#EEF2F7")],
          darkcolor=[("disabled", BG), ("pressed", "#E3E8EF"), ("active", "#EEF2F7")],
          foreground=[("disabled", DISABLED_FG)],
          bordercolor=[("disabled", BORDER), ("focus", PRIMARY)])
    s.configure("Accent.TButton", background=PRIMARY, foreground="white", bordercolor=PRIMARY,
                lightcolor=PRIMARY, darkcolor=PRIMARY, font=font(1, bold=True), padding=(22, 6))
    s.map("Accent.TButton",
          background=[("disabled", DISABLED_BG), ("pressed", PRIMARY_DARK), ("active", PRIMARY_DARK)],
          lightcolor=[("disabled", DISABLED_BG), ("active", PRIMARY_DARK)],
          darkcolor=[("disabled", DISABLED_BG), ("active", PRIMARY_DARK)],
          bordercolor=[("disabled", DISABLED_BG)],
          foreground=[("disabled", DISABLED_FG)])
    s.configure("Small.TButton", padding=(8, 3))
    s.configure("Danger.TButton", foreground=DANGER)
    s.map("Danger.TButton", foreground=[("disabled", DISABLED_FG)])
    s.configure("Header.TButton", background=HEADER, foreground="white", bordercolor=HEADER_SUB,
                lightcolor=HEADER, darkcolor=HEADER)
    s.map("Header.TButton", background=[("disabled", HEADER), ("active", "#1A5185")],
          lightcolor=[("disabled", HEADER), ("active", "#1A5185")],
          darkcolor=[("disabled", HEADER), ("active", "#1A5185")],
          bordercolor=[("disabled", "#3B6189")], foreground=[("disabled", HEADER_SUB)])

    # 输入框
    for w in ("TEntry", "TSpinbox"):
        s.configure(w, fieldbackground=CARD, background=CARD, bordercolor="#C9D1DC", lightcolor=CARD,
                    darkcolor=CARD, padding=(6, 3), arrowcolor=MUTED)
        s.map(w, bordercolor=[("focus", PRIMARY)], lightcolor=[("focus", PRIMARY)])
    s.configure("Card.TCheckbutton", background=CARD, indicatorbackground=CARD, indicatormargin=(0, 0, 6, 0))
    s.map("Card.TCheckbutton", background=[("active", CARD)],
          indicatorbackground=[("selected", PRIMARY), ("active", "#EEF2F7")],
          indicatorforeground=[("selected", "white")])

    # 进度条
    s.configure("Horizontal.TProgressbar", background=PRIMARY, troughcolor="#E8EDF4", bordercolor="#E8EDF4",
                lightcolor=PRIMARY, darkcolor=PRIMARY, thickness=scaled(root, 10))

    # 表格（处理记录窗口）
    line = tkfont.Font(root, font=font()).metrics("linespace")
    s.configure("Treeview", background=CARD, fieldbackground=CARD, bordercolor=BORDER, rowheight=line + 8)
    s.map("Treeview", background=[("selected", "#DCE9FA")], foreground=[("selected", TEXT)])
    s.configure("Treeview.Heading", background="#EEF2F7", foreground=TEXT, font=font(0, bold=True),
                bordercolor=BORDER, lightcolor="#EEF2F7", darkcolor="#EEF2F7", padding=(6, 5))
    s.map("Treeview.Heading", background=[("active", "#E3E8EF")])
    s.configure("Vertical.TScrollbar", background=BG, troughcolor=CARD, bordercolor=CARD,
                lightcolor=BG, darkcolor=BG, arrowcolor=MUTED)


def scaled(root, px):
    """按屏幕 DPI 放大像素尺寸（Windows 高分屏 150% 时，10 → 15）。"""
    try:
        return max(1, round(px * root.winfo_fpixels("1i") / 96))
    except tk.TclError:
        return px


def card(parent, padding=14):
    """白底细边框的卡片，返回内部可放控件的 Frame。"""
    outer = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER, highlightcolor=BORDER)
    inner = ttk.Frame(outer, style="Card.TFrame", padding=padding)
    inner.pack(fill="both", expand=True)
    return outer, inner


def style_text(widget):
    """日志 / 说明文字框的样式和高亮标签。"""
    widget.configure(bg=LOG_BG, fg=TEXT, font=mono(), relief="flat", borderwidth=0,
                     highlightthickness=0, padx=10, pady=8, insertbackground=TEXT,
                     selectbackground="#DCE9FA", selectforeground=TEXT, spacing1=1, spacing3=1)
    widget.tag_configure("error", foreground=DANGER, font=mono() + ("bold",))
    widget.tag_configure("warn", foreground=WARN)
    widget.tag_configure("ok", foreground=OK)
    widget.tag_configure("head", foreground=HEADER, font=mono() + ("bold",))
    widget.tag_configure("muted", foreground=MUTED)


_PROGRESS_LINE = re.compile(r"^\[\d+/\d+\] ")


def line_tag(line):
    """按一行日志的内容决定颜色。"""
    s = line.strip()
    if s.startswith(("[错误]", "[严重]", "[校验未通过]")):
        return "error"
    if s.startswith(("[警告]", "[已排除]", "[提示]", "[提速建议]", "提醒：", "[排除]")):
        return "warn"
    if s.startswith("=="):
        return "head"
    if _PROGRESS_LINE.match(s):  # [3/120] P0003: 完成，写出 …
        return "warn" if any(w in s for w in ("警告", "错误", "失败", "未完成", "停止")) else "ok"
    if s.startswith(("全部完成", "报告已保存", "探查完成")):
        return "ok"
    if s.startswith(("处理未完成", "处理完成，但")):
        return "warn"
    return None
