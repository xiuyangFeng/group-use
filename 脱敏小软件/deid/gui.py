"""图形界面（tkinter，Windows / Mac 通用，无需额外安装）。配色、字体和自绘小部件见 theme.py。

布局：左边“第 1 步 选择路径 → 第 2 步 选项 → 开始”，右边“运行状态 + 日志”。
"""

import os
import queue
import re
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import theme, vault_link
from .history import last_deid_run, load_runs, open_path
from .pipeline import EMPTY, MAX_WORKERS, SINGLE, Control, Job, detect_input_mode, is_disk_full


def _explain(e):
    """把常见的系统错误翻成大白话。"""
    if is_disk_full(e):
        return f"磁盘空间不足（Errno 28）。请清出空间或换一个盘。详细：{e}"
    return str(e)


MAPPING_NAME = "脱敏映射表_勿外发.xlsx"


# ---------------------------------------------------------------- 路径检查（只读，只看第一层）

def _inside(a, b):
    try:
        return a == b or a.is_relative_to(b)
    except (ValueError, OSError):
        return False


def check_paths(inp, out, mp, single_override=None):
    """三个路径各自的检查结果，加上判断出的输入文件夹结构。

    返回 (res, layout, single)：
      res    [(kind, 文字)]，kind: ok / info / warn / error / idle
      layout pipeline.InputLayout（路径不可用时为 None）
      single 最终采用的模式：single_override 为 None 时按 layout 自动判断
    只读，不创建任何东西。
    """
    pi = Path(inp).expanduser() if inp else None
    po = Path(out).expanduser() if out else None
    pm = Path(mp).expanduser() if mp else None
    layout = None
    if pi is not None and pi.is_dir():
        try:
            layout = detect_input_mode(pi)
        except OSError:
            layout = None
    if single_override is not None:
        single = bool(single_override)
    else:
        single = bool(layout is not None and layout.mode == SINGLE)
    res = _check_plain(pi, po, pm, single, layout, auto=single_override is None)
    # 「文件保险箱」：锁着的保险箱里的东西看起来“不存在”，要先解锁；输出放在保险箱里会跟着被加密
    for i, (path, what) in enumerate(((pi, "原始数据"), (po, "输出文件夹"), (pm, "映射表"))):
        if not path or res[i][0] == "idle":
            continue
        problem = vault_link.locked_problem(path, what)
        root = vault_link.find_vault(path)
        if problem:
            res[i] = ("error", problem)
        elif root is not None and i == 1 and res[i][0] != "error":
            res[i] = ("warn", f"在保险箱「{root.name}」里：锁回时脱敏结果也会被加密。要发出去的数据建议放在保险箱外面")
        elif root is not None and res[i][0] in ("ok", "info"):
            res[i] = (res[i][0], f"{res[i][1]}（在已解锁的保险箱「{root.name}」里，处理期间不会自动上锁）")
    return res, layout, single


def _check_plain(pi, po, pm, single=False, layout=None, auto=True):
    res = []

    if not pi:
        res.append(("idle", "还没有选择"))
    elif not pi.is_dir():
        res.append(("error", "找不到这个文件夹（移动硬盘没插？）"))
    elif single:
        if layout is not None and layout.reason == "mixed_root":
            res.append(("error", "根目录里的 DICOM 文件属于多个患者 ID，不能按单个患者处理；"
                                 "请把每个患者的文件分别放进各自的文件夹"))
        elif auto:
            why = {"root_dicoms": "自动判断：文件夹里直接有 DICOM 文件",
                   "same_patient": "自动判断：各子文件夹里的 DICOM 属于同一个患者"}.get(
                       layout.reason if layout is not None else "", "自动判断")
            res.append(("ok", f"单个患者模式：把选中的文件夹当作一个患者（{why}）"))
        else:
            res.append(("ok", "单个患者模式：把选中的文件夹当作一个患者（手动指定；"
                              "如果里面有多个患者，开始时会拒绝，不会混在一起）"))
    elif layout is None or layout.mode == EMPTY:
        res.append(("warn", "里面没有找到 DICOM 文件：请选“装着各个患者文件夹”的那一层；"
                            "只处理一个患者时，勾选下面“只处理一个患者”"))
    elif layout.reason == "mixed_root" and not layout.children:
        res.append(("error", "根目录里的 DICOM 文件属于多个患者 ID，不能按单个患者处理；"
                             "也没有子文件夹可按批量处理。请把每个患者的文件分别放进各自的文件夹"))
    elif not layout.children:
        res.append(("warn", "里面没有子文件夹：只处理一个患者时，请勾选下面“只处理一个患者”"))
    else:
        note = ""
        if layout.mode == SINGLE:
            note = "（当前按批量处理；这个文件夹看着像单个患者，取消勾选下面“只处理一个患者”就按单个患者处理）"
        elif layout.reason == "mixed_root":
            note = "（根目录下还有 DICOM 文件，批量模式下会被忽略；如果它们也是患者数据，请勾选下面“只处理一个患者”）"
        elif layout.reason == "same_patient":
            note = "（子文件夹里的 DICOM 都属于同一个患者：如果选中的文件夹本身就是一个患者，勾选下面“只处理一个患者”更合适）"
        elif layout.reason == "no_pid":
            note = "（子文件夹里没读到患者 ID，先按批量处理；如果这其实是同一个患者的多次检查，请勾选下面“只处理一个患者”）"
        res.append(("warn" if note else "ok", f"找到 {layout.children} 个子文件夹（每个是一个患者）{note}"))

    if not po:
        res.append(("idle", "还没有选择"))
    elif pi and (_inside(po, pi) or _inside(pi, po)):
        res.append(("error", "不能和原始数据文件夹相同或互相包含"))
    elif po.is_dir():
        try:
            n = sum(1 for e in os.scandir(po) if e.is_dir() and re.fullmatch(r"P\d+", e.name))
        except OSError:
            n = 0
        res.append(("info", f"已存在，里面有 {n} 个患者编号：会接着处理，已完成的跳过、不覆盖") if n
                   else ("ok", "已存在（空文件夹）"))
    elif po.exists():
        res.append(("error", "这是一个文件，不是文件夹"))
    else:
        res.append(("ok", "还不存在，开始时自动新建"))

    if not pm:
        res.append(("idle", "还没有选择"))
    elif pm.suffix.lower() != ".xlsx":
        res.append(("error", "映射表需要是 .xlsx 文件"))
    elif po and _inside(pm, po):
        res.append(("error", "不能放在输出文件夹里（输出是要发出去的）"))
    elif pi and _inside(pm, pi):
        res.append(("error", "不要放在原始数据文件夹里"))
    elif pm.is_file():
        res.append(("info", "已存在：同一患者沿用原编号，新患者接着往下排"))
    else:
        res.append(("ok", "还不存在，开始时自动新建"))
    return res


# ---------------------------------------------------------------- 右侧状态卡片

_ETA = re.compile(r"剩余约([^，,]+)(?:[，,]\s*(\d+)\s*个/秒)?")


def _fmt_int(v):
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "—"


def _result_kind(result):
    result = str(result or "")
    if result.startswith("全部完成"):
        return "ok"
    if result.startswith(("未正常结束", "出错")):
        return "error"
    return "warn"


class StatusPanel:
    """右上角的状态卡片：欢迎 / 上次结果 / 运行中 / 完成。"""

    def __init__(self, app, parent):
        self.app = app
        outer, c = theme.card(parent, padding=(20, 16, 20, 16))
        self.frame = outer
        c.columnconfigure(0, weight=1)
        self.c = c

        top = ttk.Frame(c, style="Card.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        self.dot = theme.Dot(top, "idle", size=10)
        self.dot.grid(row=0, column=0, padx=(0, 10))
        self.title = ttk.Label(top, text="", style="StateTitle.TLabel")
        self.title.grid(row=0, column=1, sticky="w")
        self.btns = ttk.Frame(top, style="Card.TFrame")
        self.btns.grid(row=0, column=2, sticky="e")
        self.pause_btn = ttk.Button(self.btns, text="暂停", style="Small.TButton", command=app.toggle_pause)
        self.stop_btn = ttk.Button(self.btns, text="停止", style="Danger.TButton", command=app.stop)
        self.stop_btn.configure(padding=(10, 4))
        self.sub = ttk.Label(c, text="", style="Hint.TLabel", wraplength=1)
        self.sub.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        c.bind("<Configure>", lambda e: self.sub.configure(wraplength=max(200, e.width - 10)))

        # 进度条 + 百分比
        self.prog_row = ttk.Frame(c, style="Card.TFrame")
        self.prog_row.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(self.prog_row, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")
        self.percent = ttk.Label(self.prog_row, text="", style="Percent.TLabel", width=5, anchor="e")
        self.percent.grid(row=0, column=1, padx=(14, 0))

        # 数字卡片
        self.stats = tk.Frame(c, bg=theme.CARD)
        self.tiles = []
        for i in range(4):
            self.stats.columnconfigure(i, weight=1, uniform="stat")
            t = tk.Frame(self.stats, bg=theme.STRIPE, highlightthickness=1, highlightbackground=theme.BORDER)
            v = tk.Label(t, text="", bg=theme.STRIPE, fg=theme.TEXT, font=theme.font(7, bold=True), anchor="w")
            cap = tk.Label(t, text="", bg=theme.STRIPE, fg=theme.MUTED, font=theme.font(-1), anchor="w")
            v.pack(fill="x", padx=12, pady=(8, 0))
            cap.pack(fill="x", padx=12, pady=(0, 8))
            self.tiles.append((t, v, cap))

        # 提示行（多行，每行一个圆点）
        self.notes = ttk.Frame(c, style="Card.TFrame")
        # 操作按钮
        self.actions = ttk.Frame(c, style="Card.TFrame")

    # ---- 基本元素
    def _head(self, title, kind, sub=""):
        self.dot.set(kind)
        self.title.configure(text=title)
        self.sub.configure(text=sub)
        self.sub.grid() if sub else self.sub.grid_remove()

    def _set_stats(self, items, color=None):
        """items: [(数值, 说明)]，最多 4 个；空列表则隐藏。"""
        for i, (t, v, cap) in enumerate(self.tiles):
            if i < len(items):
                v.configure(text=items[i][0], fg=(color or {}).get(i, theme.TEXT))
                cap.configure(text=items[i][1])
                t.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            else:
                t.grid_remove()
        if items:
            self.stats.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        else:
            self.stats.grid_remove()

    def _set_progress(self, show, kind=None):
        if show:
            self.prog_row.grid(row=2, column=0, sticky="ew", pady=(14, 0))
            self.progress.configure(style=f"{kind}.Horizontal.TProgressbar" if kind else "Horizontal.TProgressbar")
        else:
            self.prog_row.grid_remove()

    def _set_notes(self, notes):
        """notes: [(kind, 文字)]"""
        for w in self.notes.winfo_children():
            w.destroy()
        for i, (kind, text) in enumerate(notes):
            theme.Dot(self.notes, kind, size=7).grid(row=i, column=0, sticky="n", pady=(theme.scaled(self.c, 7), 0))
            lab = ttk.Label(self.notes, text=text, style=f"Check{kind}.TLabel" if kind in ("warn", "error")
                            else "Hint.TLabel", wraplength=theme.scaled(self.c, 520), justify="left")
            lab.grid(row=i, column=1, sticky="w", padx=(8, 0), pady=1)
        if notes:
            self.notes.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        else:
            self.notes.grid_remove()

    def _set_actions(self, actions):
        """actions: [(文字, 路径或回调)]；路径不存在的按钮置灰。"""
        for w in self.actions.winfo_children():
            w.destroy()
        for text, target in actions:
            if callable(target):
                b = ttk.Button(self.actions, text=text, style="Small.TButton", command=target)
            else:
                b = ttk.Button(self.actions, text=text, style="Small.TButton",
                               command=lambda p=target: self.app.open_target(p))
                if not target or not Path(target).exists():
                    b.state(["disabled"])
            b.pack(side="left", padx=(0, 8))
        if actions:
            self.actions.grid(row=5, column=0, sticky="w", pady=(16, 0))
        else:
            self.actions.grid_remove()

    def _running_buttons(self, show):
        if show:
            self.pause_btn.pack(side="left", padx=(0, 8))
            self.stop_btn.pack(side="left")
        else:
            self.pause_btn.pack_forget()
            self.stop_btn.pack_forget()

    # ---- 各种状态
    def show_welcome(self):
        self._running_buttons(False)
        self._head("准备就绪", "idle", "在左边选好三个路径，然后点“开始脱敏”。")
        self._set_progress(False)
        self._set_stats([])
        self._set_notes([("ok", "原始数据只读：不修改、不移动、不删除任何文件"),
                         ("ok", "只处理一个患者：直接把那个患者的文件夹选上，软件会自动识别，不用选上一层"),
                         ("ok", "每个文件写出前都会再校验一遍，校验不通过的不写出"),
                         ("ok", "可以随时暂停或停止；下次用同样的路径再点开始，会从停下的地方接着做"),
                         ("warn", "映射表和日志里有患者信息，只保存在本机，不要随数据发出去")])
        self._set_actions([])

    def show_record(self, r, fresh=False):
        """显示一次脱敏的结果（启动时显示上次的，处理完显示这次的）。"""
        self._running_buttons(False)
        result = str(r.get("result") or "未正常结束")
        kind = _result_kind(result)
        when = r.get("end") or r.get("start") or ""
        prefix = "本次处理" if fresh else "上次处理"
        title, extra = (result, "") if len(result) <= 10 else (result.split("（")[0], result[len(result.split("（")[0]):])
        self._head(title, kind, f"{prefix} · {when}" + (f" · {extra.strip('（）')}" if extra else "")
                   + f"\n原始数据：{theme.elide_middle(str(r.get('input', '')), 80)}")
        self._set_progress(False)
        done = (r.get("written") or 0) + (r.get("existing") or 0)
        stats = [(_fmt_int(r.get("patients")), "个患者编号")]
        if "files_total" in r:
            stats.append((_fmt_int(done) if "written" in r else "—", f"已输出 / 共 {_fmt_int(r.get('files_total'))}"))
        stats.append((_fmt_int(r.get("excluded_files") or 0), "排除截图类文件"))
        stats.append((_fmt_int(r.get("skipped") or 0), "跳过（无 DICOM）"))
        self._set_stats(stats, {1: theme.OK} if kind == "ok" else {1: theme.WARN})
        notes = []
        if result.startswith(("未完成", "未正常结束")):
            notes.append(("warn", "没有处理完：直接点“开始脱敏”会接着处理，已完成的文件跳过、不覆盖"))
        elif kind != "ok":
            notes.append(("warn", "有警告或错误，请打开日志查看是哪些患者"))
        if not fresh:
            notes.append(("idle", "已自动填好这次的三个路径。换一批数据：重新选原始数据文件夹即可，映射表建议一直用同一个"))
        notes.append(("warn", "映射表和日志里有患者信息，只能留在本机"))
        self._set_notes(notes)
        self._set_actions([("打开输出文件夹", r.get("output")), ("打开日志", r.get("log")),
                           ("打开映射表", r.get("mapping"))])

    def show_running(self, kind_text):
        self._running_buttons(True)
        self.pause_btn.configure(text="暂停")
        self.pause_btn.state(["!disabled"])
        self.stop_btn.state(["!disabled"])
        self._head(kind_text, "info", "准备中……")
        self.progress.configure(value=0, maximum=1)
        self.percent.configure(text="")
        self._set_progress(True)
        self._set_stats([])
        self._set_notes([])
        self._set_actions([])

    def update_progress(self, stage, done, total, paused=False):
        self.progress.configure(maximum=max(total, 1), value=done)
        self.percent.configure(text=f"{done * 100 // total}%" if total else "")
        m = _ETA.search(stage)
        if stage.startswith("脱敏") and m:
            self.sub.configure(text="正在逐个文件脱敏并校验")
            self._set_stats([(f"{_fmt_int(done)}", f"已处理 / 共 {_fmt_int(total)}"),
                             (m.group(2) or "—", "个文件/秒"), (m.group(1).strip(), "预计剩余")])
        elif not paused:
            self.sub.configure(text=stage)
            self._set_stats([(f"{_fmt_int(done)}", f"已完成 / 共 {_fmt_int(total)}")] if total else [])
        self.sub.grid()

    def show_paused(self, paused):
        self.pause_btn.configure(text="继续" if paused else "暂停")
        self.dot.set("warn" if paused else "info")
        self.title.configure(text="已暂停" if paused else self.app.run_title)
        self.sub.configure(text="各进程写完手上的文件后停住，不会留下写了一半的文件" if paused else "继续处理……")
        self._set_progress(True, "warn" if paused else None)

    def show_stopping(self, closing=False):
        self.pause_btn.state(["disabled"])
        self.stop_btn.state(["disabled"])
        self.dot.set("warn")
        self.title.configure(text="正在停止……")
        self.sub.configure(text="等各进程写完当前文件" + ("，然后自动关闭窗口" if closing else ""))

    def show_simple(self, title, kind, sub="", notes=(), actions=()):
        self._running_buttons(False)
        self._head(title, kind, sub)
        self._set_progress(False)
        self._set_stats([])
        self._set_notes(list(notes))
        self._set_actions(list(actions))


# ---------------------------------------------------------------- 确认窗口

class ConfirmDialog:
    """开始前的确认：可滚动、带颜色，不截断。返回 True / False。"""

    def __init__(self, root, text):
        self.ok = False
        w = self.win = tk.Toplevel(root)
        w.title("确认开始脱敏")
        w.configure(bg=theme.CARD)
        w.transient(root)
        W, H = theme.scaled(root, 720), theme.scaled(root, 560)
        rx, ry = root.winfo_rootx(), root.winfo_rooty()
        w.geometry(f"{W}x{H}+{rx + max(0, (root.winfo_width() - W) // 2)}+{ry + max(0, (root.winfo_height() - H) // 3)}")
        w.minsize(theme.scaled(root, 520), theme.scaled(root, 380))

        head = ttk.Frame(w, style="Card.TFrame", padding=(24, 20, 24, 8))
        head.pack(fill="x")
        ttk.Label(head, text="请核对编号分配", style="AppTitle.TLabel").pack(anchor="w")
        ttk.Label(head, style="Hint.TLabel",
                  text="下面是将要处理的患者和对应编号。确认无误后点“开始脱敏”；不对就取消，原始数据不会有任何改动。"
                  ).pack(anchor="w", pady=(4, 0))

        foot = ttk.Frame(w, style="Card.TFrame", padding=(24, 12, 24, 20))
        foot.pack(side="bottom", fill="x")
        ttk.Button(foot, text="开始脱敏", style="Accent.TButton", command=self._yes).pack(side="right")
        ttk.Button(foot, text="取消", command=self._no).pack(side="right", padx=(0, 10))
        theme.pill(foot, "映射表和日志含患者信息，只留本机", "warn").pack(side="left")

        box = tk.Frame(w, bg=theme.CARD, highlightthickness=1, highlightbackground=theme.BORDER)
        box.pack(fill="both", expand=True, padx=24)
        t = tk.Text(box, wrap="word", state="disabled")
        theme.style_text(t)
        sb = ttk.Scrollbar(box, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        theme.fill_text(t, text)

        w.protocol("WM_DELETE_WINDOW", self._no)
        w.bind("<Escape>", lambda e: self._no())
        w.grab_set()
        w.focus_set()
        root.wait_window(w)

    def _yes(self):
        self.ok = True
        self.win.destroy()

    def _no(self):
        self.win.destroy()


# ---------------------------------------------------------------- 主窗口

class App:
    def __init__(self, root):
        self.root = root
        root.title("CT 数据脱敏工具")
        self.q = queue.Queue()
        self._auto_out = None  # 启动时从处理记录自动填入的输出路径
        self.running = False
        self.closing = False
        self.control = None
        self.run_title = ""
        self._log_path = None
        self._check_seq = 0
        # “只处理一个患者”模式：None = 按文件夹结构自动判断；True/False = 用户手动指定
        self._single_override = None
        self._setting_mode = False

        theme.apply(root)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(theme.scaled(root, 1120), int(sw * 0.94)), min(theme.scaled(root, 760), int(sh * 0.88))
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 4)}")
        root.minsize(min(theme.scaled(root, 940), w), min(theme.scaled(root, 640), h))

        self._build_header()
        body = ttk.Frame(root, padding=(20, 16, 20, 18))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=5, uniform="col")
        body.columnconfigure(1, weight=7, uniform="col")
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        self._build_left(left)
        self._build_right(right)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)
        self.show_last_run()

    # ---------- 布局
    def _build_header(self):
        header = ttk.Frame(self.root, style="Header.TFrame", padding=(20, 12, 20, 12))
        header.pack(fill="x")
        tk.Frame(self.root, bg=theme.BORDER, height=1).pack(fill="x")
        theme.Icon(header, "logo", size=38).pack(side="left")
        titles = ttk.Frame(header, style="Header.TFrame")
        titles.pack(side="left", padx=(12, 0))
        ttk.Label(titles, text="CT 数据脱敏工具", style="AppTitle.TLabel").pack(anchor="w")
        ttk.Label(titles, text="去除 DICOM 里的患者身份信息，输出可以发给外部做分割 / 标注",
                  style="AppSub.TLabel").pack(anchor="w")
        self.hist_btn = ttk.Button(header, text="处理记录", style="Header.TButton", command=self.open_history)
        self.hist_btn.pack(side="right")
        badges = ttk.Frame(header, style="Header.TFrame")
        badges.pack(side="right", padx=(0, 16))
        theme.pill(badges, "原始数据只读", "ok").pack(side="left", padx=(0, 6))
        theme.pill(badges, "离线运行 · 数据不出本机", "info").pack(side="left")

    def _step_card(self, parent, number, title, hint=None):
        outer, c = theme.card(parent, padding=(18, 12, 18, 14))
        head = ttk.Frame(c, style="Card.TFrame")
        head.pack(fill="x", pady=(0, 4))
        theme.StepBadge(head, number).pack(side="left")
        ttk.Label(head, text=title, style="CardTitle.TLabel").pack(side="left", padx=(10, 0))
        if hint:
            ttk.Label(head, text=hint, style="Hint.TLabel").pack(side="left", padx=(10, 0))
        return outer, c

    def _build_left(self, left):
        left.columnconfigure(0, weight=1)
        # 第 1 步：路径
        self.in_var, self.out_var, self.map_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        outer, c = self._step_card(left, 1, "选择路径")
        outer.grid(row=0, column=0, sticky="ew")
        c.columnconfigure(0, weight=1)
        rows = [
            ("folder", "原始数据文件夹", "每个子文件夹一个患者；只处理一个患者时，直接选那个患者文件夹", None,
             self.in_var, self.pick_input),
            ("output", "输出文件夹", "脱敏后的数据，发出去的就是它", None, self.out_var, self.pick_output),
            ("table", "映射表", "编号 ↔ 患者的对照表", "仅本机 · 勿外发", self.map_var, self.pick_mapping),
        ]
        self.checks = []
        self.path_widgets = []
        for i, (icon, label, hint, tag, var, cmd) in enumerate(rows):
            if i:
                tk.Frame(c, bg=theme.BORDER, height=1).pack(fill="x", pady=(8, 8))
            row = ttk.Frame(c, style="Card.TFrame")
            row.pack(fill="x")
            row.columnconfigure(1, weight=1)
            theme.Icon(row, icon, size=30).grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 12))
            title = ttk.Frame(row, style="Card.TFrame")
            title.grid(row=0, column=1, columnspan=2, sticky="w")
            ttk.Label(title, text=label, style="Field.TLabel").pack(side="left")
            ttk.Label(title, text=hint, style="Hint.TLabel").pack(side="left", padx=(8, 0))
            if tag:
                theme.pill(title, tag, "warn").pack(side="left", padx=(8, 0))
            e = ttk.Entry(row, textvariable=var)
            e.grid(row=1, column=1, sticky="ew", pady=(6, 0))
            b = ttk.Button(row, text="选择…", command=cmd, style="Small.TButton", width=6)
            b.grid(row=1, column=2, sticky="e", padx=(8, 0), pady=(6, 0))
            chk = ttk.Label(row, text="", style="Checkidle.TLabel")
            chk.grid(row=2, column=1, columnspan=2, sticky="w", pady=(3, 0))
            theme.autowrap(chk, row, theme.scaled(self.root, 50))
            self.checks.append(chk)
            self.path_widgets += [e, b]
            if i == 0:
                # 自动判断是批量还是单个患者；判断不对可以在这里手动改
                self.single_var = tk.BooleanVar(value=False)
                self.single_cb = theme.CheckBox(
                    row, "只处理一个患者（选中的文件夹本身就是一个患者）", self.single_var,
                    hint="自动判断，一般不用改：把那个患者的文件夹直接选上就行")
                self.single_cb.grid(row=3, column=1, columnspan=2, sticky="w", pady=(6, 0))
                self.single_var.trace_add("write", self._on_mode_change)
                var.trace_add("write", self._on_input_change)
            else:
                var.trace_add("write", lambda *a: self._schedule_check())

        # 第 2 步：选项（默认收起，只显示一行摘要）
        outer, c = self._step_card(left, 2, "选项")
        outer.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        head = c.winfo_children()[0]
        self.opt_toggle = ttk.Button(head, text="修改", style="Ghost.TButton", command=self._toggle_options)
        self.opt_toggle.pack(side="right")
        self.opt_summary = ttk.Label(c, text="", style="Hint.TLabel")
        self.opt_summary.pack(anchor="w", pady=(2, 0))
        self.opt_body = body = ttk.Frame(c, style="Card.TFrame")
        self.exclude_var = tk.BooleanVar(value=True)
        self.exclude_cb = theme.CheckBox(
            body, "排除图像里印着文字的序列", self.exclude_var,
            hint="截图、三维渲染图、剂量报告等，图上可能直接印着姓名")
        self.exclude_cb.pack(fill="x", pady=(6, 0))
        line = ttk.Frame(body, style="Card.TFrame")
        line.pack(fill="x", pady=(10, 0))
        ttk.Label(line, text="并行进程数", style="Card.TLabel").pack(side="left")
        self.workers_var = tk.IntVar(value=MAX_WORKERS)
        self.workers_sb = ttk.Spinbox(line, from_=1, to=max(16, os.cpu_count() or 4), width=4,
                                      textvariable=self.workers_var)
        self.workers_sb.pack(side="left", padx=(10, 10))
        ttk.Label(line, style="Hint.TLabel",
                  text=f"本机 {os.cpu_count()} 核 · 默认用满").pack(side="left")
        hint = ttk.Label(body, style="Hint.TLabel",
                         text="默认用满本机核心，一般不用改；机械硬盘、U 盘、网络盘建议调小到 2–4（开太多反而因为磁盘寻道变慢）",
                         justify="left")
        hint.pack(anchor="w", pady=(4, 0))
        theme.autowrap(hint, c, theme.scaled(self.root, 44))
        for v in (self.exclude_var, self.workers_var):
            v.trace_add("write", lambda *a: self._update_opt_summary())
        self._update_opt_summary()

        # 操作
        bar = ttk.Frame(left)
        bar.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        bar.columnconfigure(0, weight=1)
        self.start_btn = ttk.Button(bar, text="开始脱敏", style="Accent.TButton", command=self.start)
        self.start_btn.grid(row=0, column=0, sticky="ew")
        self.probe_btn = ttk.Button(bar, text="结构探查", command=self.start_probe)
        self.probe_btn.configure(padding=(16, 10))
        self.probe_btn.grid(row=0, column=1, sticky="ns", padx=(10, 0))
        hint = ttk.Label(left, style="BgHint.TLabel", justify="left",
                         text="结构探查：只统计数据结构、不改任何文件，报告可发给开发者调整规则")
        hint.grid(row=3, column=0, sticky="w", pady=(6, 0))
        theme.autowrap(hint, left, theme.scaled(self.root, 8))

    def _toggle_options(self):
        if self.opt_body.winfo_ismapped():
            self.opt_body.pack_forget()
            self.opt_summary.pack(anchor="w", pady=(2, 0))
            self.opt_toggle.configure(text="修改")
        else:
            self.opt_summary.pack_forget()
            self.opt_body.pack(fill="x")
            self.opt_toggle.configure(text="收起")

    def _update_opt_summary(self):
        excl = bool(self.exclude_var.get())
        text = ("排除截图类序列" if excl else "不排除截图类序列（只提示）") + f" · {self._workers()} 个并行进程"
        self.opt_summary.configure(text=("✓ " if excl else "! ") + text,
                                   style="Checkok.TLabel" if excl else "Checkwarn.TLabel")

    def _build_right(self, right):
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        self.panel = StatusPanel(self, right)
        self.panel.frame.grid(row=0, column=0, sticky="ew")

        outer, logc = theme.card(right, padding=(0, 0))
        outer.grid(row=1, column=0, sticky="nsew", pady=(14, 0))
        head = ttk.Frame(logc, style="Card.TFrame", padding=(18, 10, 12, 8))
        head.pack(fill="x")
        ttk.Label(head, text="运行日志", style="CardTitle.TLabel").pack(side="left")
        self.log_btn = ttk.Button(head, text="打开日志文件", style="Ghost.TButton",
                                  command=lambda: self.open_target(self._log_path))
        self.log_btn.pack(side="right")
        self.log_btn.state(["disabled"])
        tk.Frame(logc, bg=theme.BORDER, height=1).pack(fill="x")
        box = ttk.Frame(logc, style="Card.TFrame")
        box.pack(fill="both", expand=True)
        self.logbox = tk.Text(box, height=6, state="disabled", wrap="word")
        theme.style_text(self.logbox)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 2), pady=2)
        self.logbox.pack(side="left", fill="both", expand=True)

    # ---------- 路径检查（后台线程，避免网络盘卡住界面）
    def _schedule_check(self):
        self._check_seq += 1
        seq = self._check_seq
        self.root.after(250, lambda: self._run_check(seq))

    def _on_input_change(self, *_):
        """换了原始数据文件夹：清掉手动指定，重新自动判断批量 / 单个患者。"""
        self._single_override = None
        self._schedule_check()

    def _on_mode_change(self, *_):
        """用户勾选/取消“只处理一个患者”：记录下来，重新检查。"""
        if self._setting_mode:
            return
        self._single_override = bool(self.single_var.get())
        self._schedule_check()

    def _run_check(self, seq):
        if seq != self._check_seq:
            return
        args = (self.in_var.get().strip(), self.out_var.get().strip(), self.map_var.get().strip())
        override = self._single_override

        def work():
            try:
                res, layout, single = check_paths(*args, single_override=override)
            except Exception as e:  # 检查只是提示，出错不影响使用
                res, single = [("idle", f"无法检查：{e}")] * 3, bool(override)
            self.q.put(("checks", (seq, res, single)))
        threading.Thread(target=work, daemon=True).start()

    def _show_checks(self, seq, res, single):
        if seq != self._check_seq:
            return
        icons = {"ok": "✓ ", "info": "• ", "warn": "! ", "error": "✕ ", "idle": ""}
        for lab, (kind, text) in zip(self.checks, res):
            lab.configure(text=icons.get(kind, "") + text, style=f"Check{kind}.TLabel")
        # 没有手动指定时，勾选框跟着自动判断的结果走
        if self._single_override is None and bool(self.single_var.get()) != bool(single):
            self._setting_mode = True
            try:
                self.single_var.set(bool(single))
            finally:
                self._setting_mode = False

    # ---------- 日志与处理记录
    def _append_log(self, text):
        self.logbox.config(state="normal")
        for line in text.split("\n"):
            self.logbox.insert("end", line + "\n", theme.line_tag(line) or ())
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def _set_log_path(self, path):
        self._log_path = str(path) if path else None
        self.log_btn.state(["!disabled"] if path and Path(path).exists() else ["disabled"])

    def show_last_run(self):
        """打开软件时：填好上次的路径，并显示上次处理的概况。"""
        try:
            r = last_deid_run()
        except Exception:
            r = None
        if not r:
            self.panel.show_welcome()
            self._append_log("欢迎使用。选择三个路径后点“开始脱敏”。以后每次处理都会留下记录，可点右上角“处理记录”查看。")
            self._schedule_check()
            return
        self.in_var.set(r.get("input", ""))
        self.out_var.set(r.get("output", ""))
        self.map_var.set(r.get("mapping", ""))
        self._auto_out = r.get("output", "")
        if "single_patient" in r:  # 上次是批量还是单个患者，接着用同一种模式
            self.restore_mode(bool(r.get("single_patient")))
        self.panel.show_record(r)
        self._set_log_path(r.get("log"))
        self._append_log("已自动填好上次的三个路径，上次的结果见上方。\n"
                         "点右上角“处理记录”可以查看所有历史，以及原始数据文件夹里哪些子文件夹还没处理。")

    def restore_mode(self, single):
        """把“只处理一个患者”设成指定值（从处理记录恢复），之后不自动改，换文件夹才重新判断。"""
        self._single_override = bool(single)
        self._setting_mode = True
        try:
            self.single_var.set(bool(single))
        finally:
            self._setting_mode = False
        self._schedule_check()

    def open_history(self):
        from .history_gui import HistoryWindow
        try:
            HistoryWindow(self)
        except Exception as e:
            messagebox.showwarning("提示", f"无法打开处理记录：{e}")

    def open_target(self, path):
        if not path or not Path(path).exists():
            messagebox.showwarning("提示", f"找不到：{path or '（无）'}")
            return
        if self.running and str(path) == self.map_var.get().strip():
            messagebox.showwarning("提示", "正在处理中，映射表正在写入，请处理结束后再打开")
            return
        open_path(path)

    # ---------- 选择路径
    def pick_input(self):
        d = filedialog.askdirectory(title="选择原始数据文件夹")
        if not d:
            return
        self.in_var.set(d)
        p = Path(d)
        if not self.out_var.get() or self.out_var.get() == self._auto_out:  # 换了数据，输出也跟着换
            self.out_var.set(str(p.parent / f"{p.name}_脱敏"))
        if not self.map_var.get():
            self.map_var.set(str(p.parent / MAPPING_NAME))

    def pick_output(self):
        d = filedialog.askdirectory(title="选择输出文件夹")
        if d:
            self.out_var.set(d)

    def pick_mapping(self):
        f = filedialog.asksaveasfilename(
            title="选择或新建映射表（已存在则继续追加）", defaultextension=".xlsx",
            initialfile=MAPPING_NAME, filetypes=[("Excel", "*.xlsx")], confirmoverwrite=False)  # 已存在则追加
        if f:
            self.map_var.set(f)

    # ---------- 运行
    def _workers(self):
        try:
            return max(1, int(self.workers_var.get()))
        except (tk.TclError, ValueError):
            return MAX_WORKERS

    def _lock_inputs(self, locked):
        for w in self.path_widgets + [self.workers_sb]:
            w.state(["disabled"] if locked else ["!disabled"])
        self.exclude_cb.configure_state(not locked)
        self.single_cb.configure_state(not locked)
        for b in (self.start_btn, self.probe_btn, self.hist_btn, self.opt_toggle):
            b.state(["disabled"] if locked else ["!disabled"])

    def _begin(self, title):
        self._w = self._workers()  # 在主线程读取界面值，后台线程不碰 tkinter
        self._exclude = bool(self.exclude_var.get())
        self._mode = self._single_override  # None = 让 Job 自动判断选中的文件夹是批量还是单个患者
        self.control = Control()
        self.running = True
        self.run_title = title
        self._lock_inputs(True)
        self.panel.show_running(title)
        self._set_log_path(None)
        self.logbox.config(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.config(state="disabled")

    def _end(self):
        self.running = False
        self._lock_inputs(False)

    def toggle_pause(self):
        if not self.running or self.control.stopped:
            return
        if self.control.paused:
            self.control.resume()
            self.panel.show_paused(False)
        else:
            self.control.pause()
            self.panel.show_paused(True)

    def stop(self):
        if not self.running or self.control.stopped:
            return
        if not messagebox.askyesno("停止", "确定停止？\n\n各进程会先写完当前文件再停止，已写出的文件都是完整的。\n"
                                           "以后用同一个输出文件夹和映射表再点“开始脱敏”，会从停下的地方继续。"):
            return
        self.control.stop()
        self.panel.show_stopping()

    def start(self):
        if self.running:
            return
        inp, out, mp = self.in_var.get().strip(), self.out_var.get().strip(), self.map_var.get().strip()
        if not (inp and out and mp):
            messagebox.showwarning("提示", "请先选择三个路径")
            return
        self._begin("正在脱敏")
        threading.Thread(target=self._worker, args=(inp, out, mp, self._mode), daemon=True).start()

    def start_probe(self):
        if self.running:
            return
        inp = self.in_var.get().strip()
        if not inp:
            messagebox.showwarning("提示", "请先选择原始数据文件夹")
            return
        report = filedialog.asksaveasfilename(
            title="保存结构探查报告", defaultextension=".txt",
            initialfile="结构探查报告.txt", filetypes=[("文本", "*.txt")], confirmoverwrite=False)
        if not report:
            return
        if Path(report).exists():
            messagebox.showwarning("提示", "该文件已存在。为避免覆盖，请换一个文件名。")
            return
        self._begin("正在结构探查")
        threading.Thread(target=self._probe_worker, args=(inp, report, self._mode), daemon=True).start()

    def _probe_worker(self, inp, report, single_patient):
        from .probe import ReportSaveError, probe_to_file
        try:
            text = probe_to_file(inp, report, workers=self._w, control=self.control,  # 报告已存在则报错，不覆盖
                                 single_patient=single_patient,
                                 log=lambda m: self.q.put(("status", m)),
                                 progress=lambda stage, done, total: self.q.put(("progress", (stage, done, total))))
            if text is None:
                self.q.put(("log", "结构探查已停止，未生成报告。"))
                self.q.put(("probe_done", (None, report)))
                return
            self.q.put(("log", text))
            self.q.put(("log", f"\n报告已保存：{report}\n请先自己看一遍确认没有患者信息，再发给开发者。"))
            self.q.put(("probe_done", (True, report)))
        except ReportSaveError as e:
            self.q.put(("log", e.text))
            self.q.put(("log", f"\n[错误] {e}"))
            self.q.put(("probe_done", (False, report)))
            self.q.put(("probe_save_again", e.text))
        except Exception as e:
            self.q.put(("log", f"[错误] {_explain(e)}"))
            self.q.put(("probe_done", (False, report)))

    def _save_report_again(self, text):
        """报告保存失败（多半是磁盘满了）：报告还在内存里，让用户换个位置存，不用重跑探查。"""
        from .probe import save_report
        msg = ("探查已经做完，但报告没能保存（多半是那个盘满了）。\n\n"
               "报告内容已经显示在日志里。现在选一个别的位置保存吗？（比如 C 盘桌面或 U 盘）")
        while messagebox.askyesno("报告保存失败", msg):
            report = filedialog.asksaveasfilename(
                title="换个位置保存结构探查报告", defaultextension=".txt",
                initialfile="结构探查报告.txt", filetypes=[("文本", "*.txt")], confirmoverwrite=False)
            if not report:
                return
            try:
                save_report(text, report)  # 已存在则报错，不覆盖
            except Exception as e:
                msg = f"还是没能保存：{_explain(e)}\n\n再换一个位置试试吗？"
                continue
            self._append_log(f"\n报告已保存：{report}\n请先自己看一遍确认没有患者信息，再发给开发者。")
            self._show_probe_done(True, report)
            return

    def _show_probe_done(self, ok, report):
        if ok:
            self.panel.show_simple("探查完成", "ok", f"报告：{report}",
                                   notes=[("idle", "报告只统计结构，不含姓名、ID、日期和文件夹名"),
                                          ("warn", "发给开发者前，请自己先打开看一遍")],
                                   actions=[("打开报告", report), ("打开所在文件夹", str(Path(report).parent))])
        elif ok is None:
            self.panel.show_simple("探查已停止", "warn", "没有生成报告。")
        else:
            self.panel.show_simple("探查出错", "error", "详情见下方日志。")

    def _worker(self, inp, out, mp, single_patient):
        cancelled = {}

        def confirm(text):
            ev = threading.Event()
            box = {}
            self.q.put(("confirm", (text, ev, box)))
            ev.wait()
            cancelled["v"] = not box.get("ok", False)
            return box.get("ok", False)

        record, log_path = None, None
        try:
            job = Job(inp, out, mp, workers=self._w, control=self.control, exclude_burned_in=self._exclude,
                      single_patient=single_patient,
                      log=lambda m: self.q.put(("log", m)),
                      progress=lambda stage, done, total: self.q.put(("progress", (stage, done, total))),
                      confirm=confirm)
            log_path = job.log_path
            ok = job.run()
            rid = getattr(job, "run_id", None)
            if rid:
                record = next((r for r in load_runs() if r.get("id") == rid), None)
        except Exception as e:  # 兜底，避免界面卡死
            self.q.put(("log", f"[错误] {_explain(e)}"))
            ok = False
        self.q.put(("done", {"ok": ok, "record": record, "log": log_path, "cancelled": cancelled.get("v", False)}))

    def poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self._append_log(data)
                elif kind == "checks":
                    self._show_checks(*data)
                elif kind == "progress":
                    stage, done, total = data
                    paused = bool(self.control and (self.control.paused or self.control.stopped))
                    self.panel.update_progress(stage, done, total, paused=paused)
                elif kind == "confirm":
                    text, ev, box = data
                    box["ok"] = ConfirmDialog(self.root, text).ok
                    ev.set()
                elif kind == "status":
                    if not (self.control and (self.control.paused or self.control.stopped)):
                        self.panel.sub.configure(text=data)
                elif kind == "probe_save_again":
                    if not self.closing:
                        self._save_report_again(data)
                elif kind == "probe_done":
                    self._end()
                    if self.closing:
                        self.root.destroy()
                        return
                    self._show_probe_done(*data)
                elif kind == "done":
                    self._end()
                    if self.closing:
                        self.root.destroy()
                        return
                    self._on_done(data)
                    self._schedule_check()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def _on_done(self, d):
        if d["log"]:
            self._set_log_path(d["log"])
        r = d["record"]
        if d["cancelled"]:
            self.panel.show_simple("已取消", "idle", "没有写出任何文件，原始数据没有任何改动。")
            return
        if r is None:  # 还没到脱敏这一步就结束了（路径不对、映射表打不开等）
            self.panel.show_simple("没有开始处理", "error", "检查没有通过，原因见下方日志。",
                                   actions=[("打开日志", d["log"])] if d["log"] else [])
            return
        self.panel.show_record(r, fresh=True)
        if self.control.stopped:
            messagebox.showinfo("已停止", "已停止。已写出的文件都是完整的。\n\n"
                                          "用同一个输出文件夹和映射表再点“开始脱敏”，会从停下的地方继续。")
        elif d["ok"]:
            skipped = r.get("skipped") or 0
            messagebox.showinfo("完成", "全部完成，校验通过。"
                                + (f"\n\n另有 {skipped} 个文件夹里没有 DICOM 文件（比如只有图片），"
                                   "已跳过，日志里有记录。" if skipped else "")
                                + "\n\n映射表和日志含患者信息，请勿外发。")
        else:
            messagebox.showwarning("完成", "处理结束，但有警告或错误，请查看日志。")

    def on_close(self):
        if not self.running:
            self.root.destroy()
            return
        if self.closing:
            return
        if not messagebox.askyesno("退出", "正在处理中。确定停止并退出？\n\n会先等各进程写完当前文件，再关闭窗口。"):
            return
        # 先正常停止，等后台进程都结束再关窗口，避免关了界面后台还在写文件
        self.closing = True
        self.control.stop()
        self.panel.show_stopping(closing=True)


def run_gui():
    if sys.platform == "win32":
        try:  # 高分屏下字体清晰
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()
