"""“处理记录”窗口：以前每次脱敏 / 探查的情况，以及原始数据文件夹里哪些子文件夹已经处理过。只读。"""

import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import theme
from .history import folder_status, history_path, load_runs, open_path, summarize

RUN_COLS = (("start", "开始时间", 150), ("kind", "类型", 70), ("result", "结果", 170),
            ("input", "原始数据文件夹", 300), ("count", "患者/文件夹", 90), ("files", "写出/总文件", 110))


class HistoryWindow:
    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("处理记录")
        sh = self.win.winfo_screenheight()
        self.win.geometry(f"{theme.scaled(app.root, 1080)}x{min(theme.scaled(app.root, 760), int(sh * 0.85))}")
        self.win.configure(bg=theme.BG)
        self.runs = load_runs()

        frm = ttk.Frame(self.win, padding=14)
        frm.pack(fill="both", expand=True)
        head = ttk.Frame(frm)
        head.pack(fill="x")
        ttk.Label(head, style="BgHint.TLabel",
                  text=f"记录保存在 {history_path()}\n（只有时间、路径和数量，不含患者信息；每个患者的明细在映射表里）"
                  ).pack(side="left")
        ttk.Button(head, text="用主窗口当前的原始数据文件夹和映射表核对",
                   command=self.check_current_paths).pack(side="right")

        top = ttk.Frame(frm)
        top.pack(fill="x", pady=(8, 0))
        self.runs_tv = self._tree(top, RUN_COLS, height=6)
        for i, r in enumerate(self.runs):
            self.runs_tv.insert("", "end", iid=str(i), values=self._run_values(r), tags=(self._run_tag(r),))
        self.runs_tv.bind("<<TreeviewSelect>>", lambda e: self.show_run())

        self.info = tk.Text(frm, height=8, state="disabled", wrap="word")
        theme.style_text(self.info)
        self.info.configure(highlightthickness=1, highlightbackground=theme.BORDER)
        self.info.pack(fill="x", pady=(10, 8))

        btns = ttk.Frame(frm)
        btns.pack(fill="x")
        self.buttons = [
            ttk.Button(btns, text="这次处理了哪些文件夹", command=self.show_batch),
            ttk.Button(btns, text="核对原始数据文件夹（哪些还没处理）", command=self.show_folder_status),
            ttk.Button(btns, text="填入这组路径", command=self.use_paths),
            ttk.Button(btns, text="打开日志/报告", command=lambda: self.open_field("log")),
            ttk.Button(btns, text="打开映射表", command=lambda: self.open_field("mapping")),
            ttk.Button(btns, text="打开输出文件夹", command=lambda: self.open_field("output")),
        ]
        for b in self.buttons:
            b.configure(style="Small.TButton")
            b.pack(side="left", padx=(0, 6))
            b.state(["disabled"])

        bottom = ttk.Frame(frm)
        bottom.pack(fill="both", expand=True, pady=(6, 0))
        self.detail_title = ttk.Label(bottom, text="", font=theme.font(0, bold=True))
        self.detail_title.pack(anchor="w", pady=(0, 4))
        self.detail_tv = self._tree(bottom, (), height=6)

        if self.runs:
            self.runs_tv.selection_set("0")
        else:
            self._set_info("还没有处理记录（这个版本开始才有记录）。\n"
                           "以前用旧版本处理过的数据，可以在主窗口填好原始数据文件夹和映射表，"
                           "再点右上角的按钮核对哪些子文件夹已经处理过。")

    # ---------- 工具
    @staticmethod
    def _tree(parent, cols, height):
        box = ttk.Frame(parent)
        box.pack(fill="both", expand=True)
        tv = ttk.Treeview(box, show="headings", height=height)
        sb = ttk.Scrollbar(box, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        tv.tag_configure("good", foreground=theme.OK)
        tv.tag_configure("warn", foreground=theme.WARN)
        tv.tag_configure("bad", foreground=theme.DANGER)
        sb.pack(side="right", fill="y")
        HistoryWindow._set_columns(tv, cols)
        return tv

    @staticmethod
    def _set_columns(tv, cols):
        tv.delete(*tv.get_children())
        tv["columns"] = [c[0] for c in cols]
        for key, title, width in cols:
            tv.heading(key, text=title)
            tv.column(key, width=width, stretch=key in ("input", "folder", "note"))

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
        self.info.config(state="normal")
        self.info.delete("1.0", "end")
        self.info.insert("end", text)
        self.info.config(state="disabled")

    def _run(self):
        sel = self.runs_tv.selection()
        return self.runs[int(sel[0])] if sel else None

    # ---------- 动作
    def show_run(self):
        r = self._run()
        if not r:
            return
        self._set_info(summarize(r))
        is_deid = r.get("kind") == "脱敏"
        for b in self.buttons:
            b.state(["!disabled"])
        if not is_deid:
            for b in self.buttons[:3] + self.buttons[4:]:
                b.state(["disabled"])
        self._set_columns(self.detail_tv, ())
        self.detail_title.config(text="")

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
        self.show_folder_status({"input": inp, "mapping": mp})

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
        try:
            items = folder_status(inp, r["mapping"])
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
        self.detail_title.config(
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
