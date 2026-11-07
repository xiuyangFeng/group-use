"""“处理记录”窗口：以前每次脱敏 / 探查的情况，以及原始数据文件夹里哪些子文件夹已经处理过。只读。"""

import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import theme
from .history import folder_status, load_runs, open_path, summarize

RUN_COLS = (("start", "开始时间", 150), ("kind", "类型", 70), ("result", "结果", 330),
            ("input", "原始数据文件夹", 240), ("count", "患者/文件夹", 90), ("files", "写出/总文件", 110))


class HistoryWindow:
    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("处理记录")
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"{min(theme.scaled(app.root, 1120), int(sw * 0.94))}x"
                          f"{min(theme.scaled(app.root, 800), int(sh * 0.88))}")
        self.win.minsize(theme.scaled(app.root, 860), theme.scaled(app.root, 600))
        self.win.configure(bg=theme.BG)
        self.runs = load_runs()

        frm = ttk.Frame(self.win, padding=(20, 16, 20, 18))
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(0, weight=1)

        # ---- 标题
        head = ttk.Frame(frm)
        head.grid(row=0, column=0, sticky="ew")
        titles = ttk.Frame(head)
        titles.pack(side="left")
        ttk.Label(titles, text="处理记录", style="BgTitle.TLabel").pack(anchor="w")
        ttk.Label(titles, style="BgHint.TLabel",
                  text=f"共 {len(self.runs)} 次 · 只记时间、路径和数量，不含患者姓名/ID（每个患者的明细在映射表里）"
                  ).pack(anchor="w", pady=(2, 0))
        ttk.Button(head, text="用主窗口当前路径核对", style="Small.TButton",
                   command=self.check_current_paths).pack(side="right", anchor="s")

        # ---- 记录列表
        outer, c = theme.card(frm, padding=0)
        outer.grid(row=1, column=0, sticky="nsew", pady=(14, 0))
        frm.rowconfigure(1, weight=2)
        self.runs_tv = self._tree(c, RUN_COLS, height=6)
        for i, r in enumerate(self.runs):
            self.runs_tv.insert("", "end", iid=str(i), values=self._run_values(r),
                                tags=(self._run_tag(r), "odd" if i % 2 else "even"))
        self.runs_tv.bind("<<TreeviewSelect>>", lambda e: self.show_run())
        if not self.runs:
            tk.Label(c, text="还没有处理记录\n\n以前用旧版本处理过的数据：在主窗口填好原始数据文件夹和映射表，\n"
                             "再点右上角“用主窗口当前路径核对”，就能看出哪些子文件夹已经处理过",
                     bg=theme.CARD, fg=theme.MUTED, font=theme.font(), justify="center"
                     ).place(relx=0.5, rely=0.55, anchor="center")

        # ---- 选中记录的概况 + 操作
        outer, c = theme.card(frm, padding=(18, 14, 18, 14))
        outer.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        c.columnconfigure(0, weight=1)
        top = ttk.Frame(c, style="Card.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        self.sel_dot = theme.Dot(top, "idle")
        self.sel_dot.pack(side="left", padx=(0, 8))
        self.sel_title = ttk.Label(top, text="选中上面的一条记录查看详情", style="CardTitle.TLabel")
        self.sel_title.pack(side="left")
        self.kv = ttk.Frame(c, style="Card.TFrame")
        self.kv.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.kv.columnconfigure(1, weight=1)

        btns = ttk.Frame(c, style="Card.TFrame")
        btns.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        self.buttons = [
            ttk.Button(btns, text="这次处理了哪些文件夹", command=self.show_batch),
            ttk.Button(btns, text="核对：哪些还没处理", command=self.show_folder_status),
            ttk.Button(btns, text="填入这组路径", command=self.use_paths),
            ttk.Button(btns, text="打开日志/报告", command=lambda: self.open_field("log")),
            ttk.Button(btns, text="打开映射表", command=lambda: self.open_field("mapping")),
            ttk.Button(btns, text="打开输出文件夹", command=lambda: self.open_field("output")),
        ]
        for i, b in enumerate(self.buttons):
            b.configure(style="Small.TButton")
            b.pack(side="left", padx=(16 if i in (2, 3) else 0, 6))
            b.state(["disabled"])

        # ---- 明细表
        outer, c = theme.card(frm, padding=0)
        outer.grid(row=3, column=0, sticky="nsew", pady=(12, 0))
        frm.rowconfigure(3, weight=3)
        self.detail_title = ttk.Label(c, text="明细：点上面的“这次处理了哪些文件夹”或“核对：哪些还没处理”",
                                      style="Hint.TLabel", padding=(16, 10, 16, 8))
        self.detail_title.pack(anchor="w")
        tk.Frame(c, bg=theme.BORDER, height=1).pack(fill="x")
        self.detail_tv = self._tree(c, (), height=6)

        if self.runs:
            self.runs_tv.selection_set("0")

    # ---------- 工具
    @staticmethod
    def _tree(parent, cols, height):
        box = ttk.Frame(parent, style="Card.TFrame")
        box.pack(fill="both", expand=True)
        tv = ttk.Treeview(box, show="headings", height=height)
        sb = ttk.Scrollbar(box, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        tv.tag_configure("good", foreground=theme.OK)
        tv.tag_configure("warn", foreground=theme.WARN)
        tv.tag_configure("bad", foreground=theme.DANGER)
        tv.tag_configure("odd", background=theme.STRIPE)
        sb.pack(side="right", fill="y", padx=(0, 2), pady=2)
        HistoryWindow._set_columns(tv, cols)
        return tv

    @staticmethod
    def _set_columns(tv, cols):
        tv.delete(*tv.get_children())
        tv["columns"] = [c[0] for c in cols]
        for key, title, width in cols:
            tv.heading(key, text=title, anchor="w")
            tv.column(key, width=width, stretch=key in ("input", "folder", "note"), anchor="w")

    @staticmethod
    def _run_tag(r):
        res = str(r.get("result", ""))
        if res.startswith(("全部完成", "完成")) and "警告" not in res and "失败" not in res:
            return "good"
        if res.startswith(("未正常结束", "出错")) or "失败" in res:
            return "bad"
        return "warn"

    @staticmethod
    def _run_values(r):
        if r.get("kind") == "脱敏":
            count = f"{r.get('patients', '')}/{r.get('folders', '')}"
            files = f"{r.get('written', 0) + r.get('existing', 0)}/{r.get('files_total', '')}"
        else:
            count = files = ""
        return (r.get("start", ""), r.get("kind", ""), r.get("result", ""), r.get("input", ""), count, files)

    def _set_info(self, text):
        """把 summarize() 的多行文字排成“键：值”两列。第一行作标题。"""
        for w in self.kv.winfo_children():
            w.destroy()
        lines = text.split("\n")
        self.sel_title.configure(text=lines[0].replace("  ", " · ", 1))
        for i, line in enumerate(lines[1:]):
            k, sep, v = line.partition("：")
            if not sep:
                k, v = "", line
            ttk.Label(self.kv, text=k, style="Hint.TLabel").grid(row=i, column=0, sticky="nw", padx=(0, 14), pady=1)
            ttk.Label(self.kv, text=v, style="Card.TLabel", wraplength=theme.scaled(self.win, 900),
                      justify="left").grid(row=i, column=1, sticky="w", pady=1)

    @staticmethod
    def _stripe(tv):
        for i, iid in enumerate(tv.get_children()):
            tags = [t for t in tv.item(iid, "tags") if t != "odd"]
            tv.item(iid, tags=tags + (["odd"] if i % 2 else []))

    def _run(self):
        sel = self.runs_tv.selection()
        return self.runs[int(sel[0])] if sel else None

    # ---------- 动作
    def show_run(self):
        r = self._run()
        if not r:
            return
        self._set_info(summarize(r))
        self.sel_dot.set({"good": "ok", "warn": "warn", "bad": "error"}[self._run_tag(r)])
        is_deid = r.get("kind") == "脱敏"
        for b in self.buttons:
            b.state(["!disabled"])
        if not is_deid:
            for b in self.buttons[:3] + self.buttons[4:]:
                b.state(["disabled"])
        self._set_columns(self.detail_tv, ())
        self.detail_title.config(text="明细：点上面的“这次处理了哪些文件夹”或“核对：哪些还没处理”",
                                 style="Hint.TLabel")

    def _read_mapping_rows(self, r):
        from .mapping import read_rows
        mp = r.get("mapping", "") if r else ""
        if self.app.running:
            messagebox.showwarning("提示", "正在处理中，映射表正在写入，请处理结束后再查看", parent=self.win)
            return None
        if not Path(mp).exists():
            messagebox.showwarning("提示", f"找不到映射表：{mp}", parent=self.win)
            return None
        try:
            return read_rows(mp)
        except Exception as e:
            messagebox.showwarning("提示", f"无法读取映射表：{e}", parent=self.win)
            return None

    def show_batch(self):
        r = self._run()
        rows = self._read_mapping_rows(r) if r else None
        if rows is None:
            return
        rows = [x for x in rows if x.get("批次") == r["id"]]
        cols = (("code", "编号", 70), ("folder", "原文件夹", 260), ("status", "状态", 150),
                ("n", "DICOM文件数", 110), ("tp", "时间点", 220), ("time", "处理时间", 150))
        self._set_columns(self.detail_tv, cols)
        for x in rows:
            self.detail_tv.insert("", "end", values=(x.get("编号") or "", x.get("原文件夹") or "", x.get("状态") or "",
                                                     x.get("DICOM文件数") or "", x.get("时间点") or "",
                                                     x.get("处理时间") or ""),
                                  tags=({0: "bad", 1: "warn", 2: "good"}[self._state(x)[0]],))
        self._stripe(self.detail_tv)
        self.detail_title.config(style="CardTitle.TLabel")
        if rows:
            self.detail_title.config(text=f"这次处理的文件夹：{len(rows)} 个（来自映射表）")
        else:
            self.detail_title.config(text="映射表里没有这次的明细（旧版本做的处理没有记录批次；"
                                          "可以用“核对原始数据文件夹”查看）")

    def check_current_paths(self):
        inp, mp = self.app.in_var.get().strip(), self.app.map_var.get().strip()
        if not (inp and mp):
            messagebox.showwarning("提示", "请先在主窗口填好原始数据文件夹和映射表", parent=self.win)
            return
        self.show_folder_status({"input": inp, "mapping": mp,
                                 "single_patient": bool(self.app.single_var.get())})

    def show_folder_status(self, r=None):
        r = r or self._run()
        if not r:
            return
        inp = r.get("input", "")
        if not Path(inp).is_dir():
            messagebox.showwarning("提示", f"原始数据文件夹现在打不开（移动硬盘没插？）：\n{inp}", parent=self.win)
            return
        if self._read_mapping_rows(r) is None:
            return
        single = bool(r.get("single_patient"))
        try:
            items = folder_status(inp, r["mapping"], single_patient=single)
        except Exception as e:
            messagebox.showwarning("提示", f"核对失败：{e}", parent=self.win)
            return
        cols = (("folder", "原文件夹", 260), ("state", "情况", 110), ("code", "编号", 70),
                ("status", "最近一次状态", 160), ("time", "处理时间", 150), ("note", "备注", 200))
        self._set_columns(self.detail_tv, cols)
        n_done = n_todo = n_new = 0
        for name, row in sorted(items, key=lambda it: (self._state(it[1])[0], it[0])):
            order, state = self._state(row)
            n_done += order == 2
            n_todo += order == 0
            n_new += order == 1
            row = row or {}
            self.detail_tv.insert("", "end", values=(name, state, row.get("编号") or "", row.get("状态") or "",
                                                     row.get("处理时间") or "", row.get("备注") or ""),
                                  tags=({0: "bad", 1: "warn", 2: "good"}[order],))
        self._stripe(self.detail_tv)
        if single:
            self.detail_title.config(
                style="CardTitle.TLabel",
                text=f"{inp} 按单个患者模式处理：已完成 {n_done}，未完成/失败 {n_todo}，从未处理 {n_new}。"
                     f"（用这个映射表核对）")
        else:
            self.detail_title.config(
                style="CardTitle.TLabel",
                text=f"{inp} 现有 {len(items)} 个子文件夹：已完成 {n_done}，未完成/失败 {n_todo}，从未处理 {n_new}。"
                     f"（用这个映射表核对）")

    @staticmethod
    def _state(row):
        """(排序, 文字)：没做完的排最前，其次从未处理，最后已完成。"""
        if row is None:
            return 1, "从未处理"
        st = str(row.get("状态") or "")
        if st.startswith("完成"):
            return 2, "已完成"
        if st.startswith("跳过"):
            return 2, "已看过（无DICOM）"
        return 0, "未完成"

    def use_paths(self):
        r = self._run()
        if not r:
            return
        if self.app.running:
            messagebox.showwarning("提示", "正在处理中，不能修改路径", parent=self.win)
            return
        self.app.in_var.set(r.get("input", ""))
        self.app.out_var.set(r.get("output", ""))
        self.app.map_var.set(r.get("mapping", ""))
        if "single_patient" in r:  # 连同“批量 / 单个患者”模式一起恢复
            self.app.restore_mode(bool(r.get("single_patient")))
        messagebox.showinfo("已填入", "已把这次的三个路径填到主窗口。\n\n点“开始脱敏”会接着处理："
                                      "已完成的文件直接跳过，不会覆盖。", parent=self.win)

    def open_field(self, key):
        r = self._run()
        if r and key == "log" and r.get("kind") != "脱敏":
            key = "report"  # 结构探查没有日志，打开报告
        path = r.get(key, "") if r else ""
        if not path or not Path(path).exists():
            messagebox.showwarning("提示", f"找不到：{path or '（无）'}", parent=self.win)
            return
        if key == "mapping" and self.app.running:
            messagebox.showwarning("提示", "正在处理中，映射表正在写入，请处理结束后再打开", parent=self.win)
            return
        open_path(path)
