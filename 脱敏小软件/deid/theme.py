"""界面配色、字体和几个自绘小部件（只用 tkinter 自带的 ttk / Canvas，不需要额外安装）。"""

import re
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# ---- 颜色
BG = "#F3F5F9"          # 窗口底色
CARD = "#FFFFFF"        # 卡片
BORDER = "#E2E6EE"
BORDER_STRONG = "#CDD4DF"
TEXT = "#111827"
TEXT2 = "#374151"
MUTED = "#6B7280"
FAINT = "#9CA3AF"
PRIMARY = "#2563EB"
PRIMARY_DARK = "#1D4ED8"
PRIMARY_SOFT = "#EEF3FF"
OK = "#15803D"
OK_SOFT = "#ECFDF3"
WARN = "#B45309"
WARN_SOFT = "#FFF7E8"
DANGER = "#DC2626"
DANGER_SOFT = "#FEF2F2"
LOG_BG = "#FAFBFC"
STRIPE = "#F8FAFC"
DISABLED_BG = "#E5E7EB"
DISABLED_FG = "#9CA3AF"
# 旧名字（其他模块可能还在用）
HEADER = TEXT

KIND_COLORS = {  # kind -> (文字色, 浅底色)
    "ok": (OK, OK_SOFT), "warn": (WARN, WARN_SOFT), "error": (DANGER, DANGER_SOFT),
    "info": (PRIMARY, PRIMARY_SOFT), "idle": (MUTED, "#F1F3F7"),
}

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
    if not _PX:  # 磅值：大约 13px ≈ 10pt，按比例换算
        size = round(size * 0.75)
    f = (UI_FONT, _size(BASE + size))
    return f + ("bold",) if bold else f


def mono():
    return (MONO_FONT, _size(BASE - 1 if _PX else BASE))


def _flat(s, style, bg, fg, border, active_bg, pressed_bg=None, **kw):
    """配置一个扁平按钮样式（clam 主题下 lightcolor/darkcolor 也要跟着改，否则有立体边）。"""
    pressed_bg = pressed_bg or active_bg
    s.configure(style, background=bg, foreground=fg, bordercolor=border, lightcolor=bg, darkcolor=bg,
                focuscolor=bg, **kw)
    s.map(style,
          background=[("disabled", DISABLED_BG if bg == PRIMARY else bg), ("pressed", pressed_bg),
                      ("active", active_bg)],
          lightcolor=[("disabled", DISABLED_BG if bg == PRIMARY else bg), ("pressed", pressed_bg),
                      ("active", active_bg)],
          darkcolor=[("disabled", DISABLED_BG if bg == PRIMARY else bg), ("pressed", pressed_bg),
                     ("active", active_bg)],
          bordercolor=[("disabled", DISABLED_BG if bg == PRIMARY else BORDER)],
          foreground=[("disabled", DISABLED_FG)])


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

    # ---- 底色 / 卡片 / 文字
    s.configure("TFrame", background=BG)
    s.configure("Card.TFrame", background=CARD)
    s.configure("Header.TFrame", background=CARD)
    s.configure("TLabel", background=BG, foreground=TEXT)
    s.configure("Card.TLabel", background=CARD, foreground=TEXT)
    s.configure("CardTitle.TLabel", background=CARD, foreground=TEXT, font=font(2, bold=True))
    s.configure("Field.TLabel", background=CARD, foreground=TEXT, font=font(0, bold=True))
    s.configure("Hint.TLabel", background=CARD, foreground=MUTED, font=font(-1))
    s.configure("BgHint.TLabel", background=BG, foreground=MUTED, font=font(-1))
    s.configure("BgTitle.TLabel", background=BG, foreground=TEXT, font=font(5, bold=True))
    s.configure("AppTitle.TLabel", background=CARD, foreground=TEXT, font=font(5, bold=True))
    s.configure("AppSub.TLabel", background=CARD, foreground=MUTED, font=font(-1))
    s.configure("StateTitle.TLabel", background=CARD, foreground=TEXT, font=font(5, bold=True))
    s.configure("StatValue.TLabel", background=CARD, foreground=TEXT, font=font(9, bold=True))
    s.configure("StatCaption.TLabel", background=CARD, foreground=MUTED, font=font(-1))
    s.configure("Percent.TLabel", background=CARD, foreground=PRIMARY, font=font(9, bold=True))
    for kind, (fg, _bg) in KIND_COLORS.items():
        s.configure(f"Check{kind}.TLabel", background=CARD, foreground=fg, font=font(-1))
    # 兼容旧样式名
    s.configure("Status.TLabel", background=CARD, foreground=TEXT)

    # ---- 按钮
    s.configure("TButton", padding=(14, 6), anchor="center")
    _flat(s, "TButton", CARD, TEXT2, BORDER_STRONG, "#F3F5F9", "#E8EBF1")
    _flat(s, "Accent.TButton", PRIMARY, "white", PRIMARY, PRIMARY_DARK, "#1E40AF",
          font=font(2, bold=True), padding=(22, 10))
    _flat(s, "Small.TButton", CARD, TEXT2, BORDER_STRONG, "#F3F5F9", "#E8EBF1", padding=(10, 4))
    _flat(s, "Ghost.TButton", CARD, PRIMARY, CARD, PRIMARY_SOFT, "#E0E9FF", padding=(8, 4))
    _flat(s, "Danger.TButton", CARD, DANGER, "#F3C4C4", DANGER_SOFT, "#FDE3E3")
    _flat(s, "Header.TButton", CARD, TEXT2, BORDER_STRONG, "#F3F5F9", "#E8EBF1", padding=(14, 6))

    # ---- 输入框
    for w in ("TEntry", "TSpinbox"):
        s.configure(w, fieldbackground=CARD, background=CARD, bordercolor=BORDER_STRONG, lightcolor=CARD,
                    darkcolor=CARD, padding=(8, 5), arrowcolor=MUTED, foreground=TEXT,
                    insertcolor=TEXT)
        s.map(w, bordercolor=[("focus", PRIMARY)], lightcolor=[("focus", PRIMARY)],
              fieldbackground=[("readonly", CARD), ("disabled", BG)])
    s.configure("Card.TCheckbutton", background=CARD, indicatorbackground=CARD, indicatormargin=(0, 0, 6, 0))
    s.map("Card.TCheckbutton", background=[("active", CARD)],
          indicatorbackground=[("selected", PRIMARY), ("active", "#EEF2F7")],
          indicatorforeground=[("selected", "white")])

    # ---- 进度条
    s.configure("Horizontal.TProgressbar", background=PRIMARY, troughcolor="#E8EDF5", bordercolor="#E8EDF5",
                lightcolor=PRIMARY, darkcolor=PRIMARY, thickness=scaled(root, 8))
    for kind in ("ok", "warn", "error"):
        fg = KIND_COLORS[kind][0]
        s.configure(f"{kind}.Horizontal.TProgressbar", background=fg, troughcolor="#E8EDF5",
                    bordercolor="#E8EDF5", lightcolor=fg, darkcolor=fg, thickness=scaled(root, 8))

    # ---- 表格（处理记录窗口）
    line = tkfont.Font(root, font=font()).metrics("linespace")
    s.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT, bordercolor=BORDER,
                lightcolor=CARD, darkcolor=CARD, rowheight=line + 12)
    s.map("Treeview", background=[("selected", "#DCE7FD")], foreground=[("selected", TEXT)])
    s.configure("Treeview.Heading", background=STRIPE, foreground=MUTED, font=font(-1, bold=True),
                bordercolor=BORDER, lightcolor=STRIPE, darkcolor=STRIPE, padding=(8, 7), relief="flat")
    s.map("Treeview.Heading", background=[("active", "#EEF1F6")])

    # ---- 细滚动条（去掉上下箭头）
    for orient, sticky in (("Vertical", "ns"), ("Horizontal", "ew")):
        s.layout(f"{orient}.TScrollbar", [
            (f"{orient}.Scrollbar.trough", {"sticky": sticky, "children": [
                (f"{orient}.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        s.configure(f"{orient}.TScrollbar", background="#D5DAE3", troughcolor=CARD, bordercolor=CARD,
                    lightcolor="#D5DAE3", darkcolor="#D5DAE3", gripcount=0, arrowsize=scaled(root, 8))
        s.map(f"{orient}.TScrollbar", background=[("active", "#B8C0CC")],
              lightcolor=[("active", "#B8C0CC")], darkcolor=[("active", "#B8C0CC")])


def scaled(root, px):
    """按屏幕 DPI 放大像素尺寸（Windows 高分屏 150% 时，10 → 15）。"""
    try:
        return max(1, round(px * root.winfo_fpixels("1i") / 96))
    except tk.TclError:
        return px


def card(parent, padding=14):
    """白底细边框的卡片，返回 (外框, 内部可放控件的 Frame)。"""
    outer = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER, highlightcolor=BORDER)
    inner = ttk.Frame(outer, style="Card.TFrame", padding=padding)
    inner.pack(fill="both", expand=True)
    return outer, inner


def autowrap(label, container, minus=0):
    """让标签文字随容器宽度自动换行（避免长提示被截断）。"""
    def _fit(e):
        label.configure(wraplength=max(120, e.width - minus))
    container.bind("<Configure>", _fit, add="+")


def pill(parent, text, kind="info", bg=CARD):
    """小标签（例如“仅本机 · 勿外发”）。"""
    fg, soft = KIND_COLORS[kind]
    return tk.Label(parent, text=text, fg=fg, bg=soft, font=font(-2, bold=True), padx=7, pady=1, bd=0)


def style_text(widget):
    """日志 / 说明文字框的样式和高亮标签。"""
    widget.configure(bg=LOG_BG, fg=TEXT2, font=mono(), relief="flat", borderwidth=0,
                     highlightthickness=0, padx=14, pady=10, insertbackground=TEXT,
                     selectbackground="#DCE7FD", selectforeground=TEXT, spacing1=2, spacing3=2)
    widget.tag_configure("error", foreground=DANGER, font=mono() + ("bold",))
    widget.tag_configure("warn", foreground=WARN)
    widget.tag_configure("ok", foreground=OK)
    widget.tag_configure("head", foreground=PRIMARY, font=mono() + ("bold",), spacing1=8)
    widget.tag_configure("muted", foreground=MUTED)


_PROGRESS_LINE = re.compile(r"^\[\d+/\d+\] ")


def line_tag(line):
    """按一行日志的内容决定颜色。"""
    s = line.strip()
    if s.startswith(("[错误]", "[严重]", "[校验未通过]")):
        return "error"
    if s.startswith(("[警告]", "[已排除]", "[提示]", "[提速建议]", "提醒：", "[排除]", "[注意]", "[空间可能不足]")):
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


def fill_text(widget, text):
    """把多行文字按颜色规则写进 Text 控件（只读）。"""
    widget.config(state="normal")
    widget.delete("1.0", "end")
    for line in text.split("\n"):
        widget.insert("end", line + "\n", line_tag(line) or ())
    widget.config(state="disabled")


# ---------------------------------------------------------------- 自绘小部件（Canvas，高分屏下清晰）

class Icon(tk.Canvas):
    """线条小图标：folder / output / table / logo。"""

    def __init__(self, parent, kind, size=28, bg=CARD, color=PRIMARY, soft=PRIMARY_SOFT):
        root = parent.winfo_toplevel()
        n = scaled(root, size)
        super().__init__(parent, width=n, height=n, bg=bg, highlightthickness=0, bd=0)
        self.n, self.color, self.soft = n, color, soft
        getattr(self, f"_{kind}")()

    def _rr(self, x0, y0, x1, y1, r, **kw):
        """圆角矩形。"""
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
               x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return self.create_polygon(pts, smooth=True, **kw)

    def _bgbox(self):
        n = self.n
        self._rr(0, 0, n, n, n * 0.28, fill=self.soft, outline="")

    def _folder(self):
        n, c = self.n, self.color
        self._bgbox()
        w = max(1, round(n / 16))
        x0, y0, x1, y1 = n * .24, n * .3, n * .76, n * .72
        self.create_polygon(x0, y0, x0 + n * .17, y0, x0 + n * .22, y0 + n * .07, x1, y0 + n * .07,
                            x1, y1, x0, y1, fill="", outline=c, width=w, joinstyle="round")

    def _output(self):
        n, c = self.n, self.color
        self._bgbox()
        w = max(1, round(n / 16))
        x0, y0, x1, y1 = n * .24, n * .3, n * .76, n * .72
        self.create_polygon(x0, y0, x0 + n * .17, y0, x0 + n * .22, y0 + n * .07, x1, y0 + n * .07,
                            x1, y1, x0, y1, fill="", outline=c, width=w, joinstyle="round")
        self.create_line(n * .40, n * .52, n * .47, n * .59, n * .61, n * .44, fill=c, width=w,
                         capstyle="round", joinstyle="round")

    def _table(self):
        n, c = self.n, self.color
        self._bgbox()
        w = max(1, round(n / 16))
        x0, y0, x1, y1 = n * .26, n * .28, n * .74, n * .72
        self.create_rectangle(x0, y0, x1, y1, outline=c, width=w)
        self.create_line(x0, n * .43, x1, n * .43, fill=c, width=w)
        self.create_line(x0, n * .575, x1, n * .575, fill=c, width=w)
        self.create_line(n * .42, y0, n * .42, y1, fill=c, width=w)

    def _logo(self):
        """应用图标：蓝底圆角方块 + 白色盾牌 + 对勾。"""
        n = self.n
        self._rr(0, 0, n, n, n * 0.26, fill=PRIMARY, outline="")
        w = max(1, round(n / 14))
        cx = n / 2
        self.create_polygon(cx, n * .2, n * .74, n * .3, n * .72, n * .55, cx, n * .8, n * .28, n * .55,
                            n * .26, n * .3, fill="", outline="white", width=w, smooth=True, joinstyle="round")
        self.create_line(n * .39, n * .5, n * .47, n * .58, n * .62, n * .42, fill="white", width=w,
                         capstyle="round", joinstyle="round")


class StepBadge(tk.Canvas):
    """圆形步骤编号 ①②。"""

    def __init__(self, parent, number, bg=CARD):
        root = parent.winfo_toplevel()
        n = scaled(root, 24)
        super().__init__(parent, width=n, height=n, bg=bg, highlightthickness=0, bd=0)
        self.create_oval(1, 1, n - 1, n - 1, fill=PRIMARY, outline="")
        self.create_text(n / 2, n / 2, text=str(number), fill="white", font=font(-1, bold=True))


class Dot(tk.Canvas):
    """状态小圆点。"""

    def __init__(self, parent, kind="idle", size=10, bg=CARD):
        n = scaled(parent.winfo_toplevel(), size)
        super().__init__(parent, width=n, height=n, bg=bg, highlightthickness=0, bd=0)
        self._o = self.create_oval(1, 1, n - 1, n - 1, outline="")
        self.set(kind)

    def set(self, kind):
        self.itemconfigure(self._o, fill=KIND_COLORS.get(kind, KIND_COLORS["idle"])[0])


class CheckBox(tk.Frame):
    """自绘勾选框（ttk 的 clam 主题勾选框是个叉，不好看）。variable 为 tk.BooleanVar。"""

    def __init__(self, parent, text, variable, hint=None, bg=CARD):
        super().__init__(parent, bg=bg)
        self.var = variable
        self.enabled = True
        n = scaled(parent.winfo_toplevel(), 18)
        self.box = tk.Canvas(self, width=n, height=n, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.box.grid(row=0, column=0, sticky="n", pady=(scaled(self, 2), 0))
        self.n = n
        self.label = tk.Label(self, text=text, bg=bg, fg=TEXT, font=font(), cursor="hand2", anchor="w",
                              justify="left")
        self.label.grid(row=0, column=1, sticky="w", padx=(scaled(self, 8), 0))
        if hint:
            self.hint = tk.Label(self, text=hint, bg=bg, fg=MUTED, font=font(-1), anchor="w", justify="left")
            self.hint.grid(row=1, column=1, sticky="w", padx=(scaled(self, 8), 0))
        for w in (self.box, self.label):
            w.bind("<Button-1>", self._toggle)
        self.var.trace_add("write", lambda *a: self._draw())
        self._draw()

    def _toggle(self, _e=None):
        if self.enabled:
            self.var.set(not self.var.get())

    def configure_state(self, enabled):
        self.enabled = enabled
        self.label.configure(fg=TEXT if enabled else DISABLED_FG)
        self._draw()

    def _draw(self):
        c, n = self.box, self.n
        c.delete("all")
        on = bool(self.var.get())
        r = n * 0.25
        pts = [r, 1, n - r, 1, n - 1, 1, n - 1, r, n - 1, n - r, n - 1, n - 1, n - r, n - 1,
               r, n - 1, 1, n - 1, 1, n - r, 1, r, 1, 1]
        color = PRIMARY if self.enabled else DISABLED_FG
        if on:
            c.create_polygon(pts, smooth=True, fill=color, outline=color)
            w = max(2, round(n / 9))
            c.create_line(n * .27, n * .52, n * .43, n * .68, n * .74, n * .34, fill="white", width=w,
                          capstyle="round", joinstyle="round")
        else:
            c.create_polygon(pts, smooth=True, fill=CARD, outline=BORDER_STRONG)


def elide_middle(text, limit=60):
    """长路径中间用 … 省略。"""
    return text if len(text) <= limit else text[: limit // 2 - 1] + "…" + text[-(limit // 2):]
