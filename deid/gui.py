"""图形界面（tkinter，Windows / Mac 通用，无需额外安装）。配色和字体见 theme.py。"""

import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import theme
from .history import last_deid_run, summarize
from .pipeline import MAX_WORKERS, Control, Job, is_disk_full


def _explain(e):
    """把常见的系统错误翻成大白话。"""
    if is_disk_full(e):
        return f"磁盘空间不足（Errno 28）。请清出空间或换一个盘。详细：{e}"
    return str(e)

MAPPING_NAME = "脱敏映射表_勿外发.xlsx"


class App:
    def __init__(self, root):
        self.root = root
        root.title("CT 数据脱敏工具")
        self.q = queue.Queue()
        self._auto_out = None  # 启动时从处理记录自动填入的输出路径
        self.running = False
        self.closing = False
        self.control = None

        theme.apply(root)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(theme.scaled(root, 1000), int(sw * 0.92)), min(theme.scaled(root, 720), int(sh * 0.85))
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 4)}")
        root.minsize(min(theme.scaled(root, 820), w), min(theme.scaled(root, 600), h))

        # ---- 顶部标题栏
        header = ttk.Frame(root, style="Header.TFrame", padding=(20, 12))
        header.pack(fill="x")
        titles = ttk.Frame(header, style="Header.TFrame")
        titles.pack(side="left")
        ttk.Label(titles, text="CT 数据脱敏工具", style="Title.TLabel").pack(anchor="w")
        ttk.Label(titles, style="Subtitle.TLabel",
                  text="原始数据只读，不修改、不删除任何文件  ·  映射表和日志只保存在本机").pack(anchor="w", pady=(2, 0))
        self.hist_btn = ttk.Button(header, text="处理记录", style="Header.TButton", command=self.open_history)
        self.hist_btn.pack(side="right")

        body = ttk.Frame(root, padding=(16, 12, 16, 14))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        # ---- 路径
        self.in_var = tk.StringVar()
        self.out_var = tk.StringVar()
        self.map_var = tk.StringVar()
        outer, paths = theme.card(body, padding=(14, 8))
        outer.grid(row=0, column=0, sticky="ew")
        paths.columnconfigure(1, weight=1)
        rows = [
            ("原始数据文件夹", self.in_var, self.pick_input, "每个子文件夹是一个患者"),
            ("输出文件夹", self.out_var, self.pick_output, "脱敏后的数据，发出去的就是它"),
            ("映射表", self.map_var, self.pick_mapping, "编号↔患者对照，只留本机，勿外发"),
        ]
        for r, (label, var, cmd, hint) in enumerate(rows):
            lab = ttk.Frame(paths, style="Card.TFrame")
            lab.grid(row=r, column=0, sticky="w", pady=4, padx=(0, 14))
            ttk.Label(lab, text=label, style="Card.TLabel", font=theme.font(0, bold=True)).pack(anchor="w")
            ttk.Label(lab, text=hint, style="Hint.TLabel").pack(anchor="w")
            ttk.Entry(paths, textvariable=var).grid(row=r, column=1, sticky="ew")
            ttk.Button(paths, text="选择…", command=cmd).grid(row=r, column=2, padx=(8, 0))

        # ---- 选项
        outer, opt = theme.card(body, padding=(14, 8))
        outer.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        line1 = ttk.Frame(opt, style="Card.TFrame")
        line1.pack(fill="x")
        ttk.Label(line1, text="并行进程数", style="Card.TLabel").pack(side="left")
        self.workers_var = tk.IntVar(value=MAX_WORKERS)
        ttk.Spinbox(line1, from_=1, to=max(16, os.cpu_count() or 4), width=4,
                    textvariable=self.workers_var).pack(side="left", padx=8)
        ttk.Label(line1, style="Hint.TLabel",
                  text=f"本机 {os.cpu_count()} 核。固态硬盘可调到 6–8；机械硬盘 / U 盘 / 网络盘建议 2–4").pack(side="left")
        self.exclude_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, variable=self.exclude_var, style="Card.TCheckbutton",
                        text="排除疑似图像内印着文字的序列（截图、三维渲染图、剂量报告等，图上可能直接印着姓名）"
                        ).pack(anchor="w", pady=(6, 0))

        # ---- 操作按钮
        bar = ttk.Frame(body)
        bar.grid(row=2, column=0, sticky="ew", pady=(12, 8))
        self.start_btn = ttk.Button(bar, text="开始脱敏", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.probe_btn = ttk.Button(bar, text="结构探查", command=self.start_probe)
        self.probe_btn.pack(side="left", padx=(10, 0))
        ttk.Label(bar, text="只统计数据结构，不改任何文件", style="BgHint.TLabel").pack(side="left", padx=(8, 0))
        self.stop_btn = ttk.Button(bar, text="停止", style="Danger.TButton", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right")
        self.pause_btn = ttk.Button(bar, text="暂停", command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side="right", padx=(0, 8))

        # ---- 进度
        outer, prog = theme.card(body, padding=(14, 8))
        outer.grid(row=3, column=0, sticky="ew")
        top = ttk.Frame(prog, style="Card.TFrame")
        top.pack(fill="x")
        self.status = ttk.Label(top, text="就绪", style="Status.TLabel", anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.percent = ttk.Label(top, text="", style="Percent.TLabel")
        self.percent.pack(side="right")
        self.progress = ttk.Progressbar(prog, mode="determinate")
        self.progress.pack(fill="x", pady=(6, 0))

        # ---- 日志
        outer, logc = theme.card(body, padding=(0, 0))
        outer.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        body.rowconfigure(4, weight=1)
        ttk.Label(logc, text="运行日志", style="CardTitle.TLabel", padding=(14, 6, 14, 2)).pack(anchor="w")
        box = ttk.Frame(logc, style="Card.TFrame")
        box.pack(fill="both", expand=True)
        self.logbox = tk.Text(box, height=8, state="disabled", wrap="word")
        theme.style_text(self.logbox)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.logbox.pack(side="left", fill="both", expand=True)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)
        self.show_last_run()

    # ---------- 处理记录
    def _append_log(self, text):
        self.logbox.config(state="normal")
        for line in text.split("\n"):
            self.logbox.insert("end", line + "\n", theme.line_tag(line) or ())
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def show_last_run(self):
        """打开软件时：填好上次的路径，并显示上次处理的概况。"""
        try:
            r = last_deid_run()
        except Exception:
            r = None
        if not r:
            self._append_log("欢迎使用。选择三个路径后点“开始脱敏”。以后每次处理都会留下记录，可点“处理记录”查看。")
            return
        self.in_var.set(r.get("input", ""))
        self.out_var.set(r.get("output", ""))
        self.map_var.set(r.get("mapping", ""))
        self._auto_out = r.get("output", "")
        self._append_log("== 上次脱敏 ==\n" + summarize(r))
        self._append_log("\n已自动填好上次的三个路径。")
        result = str(r.get("result", ""))
        if result.startswith(("未完成", "未正常结束")):
            self._append_log("上次没有处理完：直接点“开始脱敏”会接着处理，已完成的文件会跳过，不会覆盖。")
        elif not result.startswith("全部完成"):
            self._append_log("上次有警告或错误，详情见上面的日志文件。")
        self._append_log("要处理别的数据：重新选原始数据文件夹，输出文件夹会跟着换；映射表建议继续用同一个，"
                         "编号会接着往下排，不会重复。")
        self._append_log("点“处理记录”可以查看所有历史，以及原始数据文件夹里哪些子文件夹还没处理。")

    def open_history(self):
        from .history_gui import HistoryWindow
        try:
            HistoryWindow(self)
        except Exception as e:
            messagebox.showwarning("提示", f"无法打开处理记录：{e}")

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

    def _begin(self):
        self._w = self._workers()  # 在主线程读取界面值，后台线程不碰 tkinter
        self._exclude = bool(self.exclude_var.get())
        self.control = Control()
        self.running = True
        self.start_btn.config(state="disabled")
        self.probe_btn.config(state="disabled")
        self.hist_btn.config(state="disabled")
        self.pause_btn.config(state="normal", text="暂停")
        self.stop_btn.config(state="normal")
        self.progress.config(value=0)
        self.percent.config(text="")
        self._set_status("准备中……")
        self.logbox.config(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.config(state="disabled")

    def _set_status(self, text, kind=None):
        """进度区的状态文字。kind: ok / warn / error / None（普通）"""
        color = {"ok": theme.OK, "warn": theme.WARN, "error": theme.DANGER}.get(kind, theme.TEXT)
        self.status.config(text=text, foreground=color)

    def _end(self):
        self.running = False
        self.start_btn.config(state="normal")
        self.probe_btn.config(state="normal")
        self.hist_btn.config(state="normal")
        self.pause_btn.config(state="disabled", text="暂停")
        self.stop_btn.config(state="disabled")

    def toggle_pause(self):
        if not self.running or self.control.stopped:
            return
        if self.control.paused:
            self.control.resume()
            self.pause_btn.config(text="暂停")
            self.status.config(text="继续处理……")
        else:
            self.control.pause()
            self.pause_btn.config(text="继续")
            self.status.config(text="已暂停（写完当前文件后停住）")

    def stop(self):
        if not self.running or self.control.stopped:
            return
        if not messagebox.askyesno("停止", "确定停止？\n\n各进程会先写完当前文件再停止，已写出的文件都是完整的。\n"
                                           "以后用同一个输出文件夹和映射表再点“开始”，会从停下的地方继续。"):
            return
        self.control.stop()
        self.pause_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")
        self.status.config(text="正在停止……")

    def start(self):
        if self.running:
            return
        inp, out, mp = self.in_var.get().strip(), self.out_var.get().strip(), self.map_var.get().strip()
        if not (inp and out and mp):
            messagebox.showwarning("提示", "请先选择三个路径")
            return
        self._begin()
        threading.Thread(target=self._worker, args=(inp, out, mp), daemon=True).start()

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
        self._begin()
        threading.Thread(target=self._probe_worker, args=(inp, report), daemon=True).start()

    def _probe_worker(self, inp, report):
        from .probe import ReportSaveError, probe_to_file
        try:
            text = probe_to_file(inp, report, workers=self._w, control=self.control,  # 报告已存在则报错，不覆盖
                                 log=lambda m: self.q.put(("status", m)),
                                 progress=lambda stage, done, total: self.q.put(("progress", (stage, done, total))))
            if text is None:
                self.q.put(("log", "结构探查已停止，未生成报告。"))
                self.q.put(("probe_done", None))
                return
            self.q.put(("log", text))
            self.q.put(("log", f"\n报告已保存：{report}\n请先自己看一遍确认没有患者信息，再发给开发者。"))
            self.q.put(("probe_done", True))
        except ReportSaveError as e:
            self.q.put(("log", e.text))
            self.q.put(("log", f"\n[错误] {e}"))
            self.q.put(("probe_done", False))
            self.q.put(("probe_save_again", e.text))
        except Exception as e:
            self.q.put(("log", f"[错误] {_explain(e)}"))
            self.q.put(("probe_done", False))

    def _save_report_again(self, text):
        """报告保存失败（多半是磁盘满了）：报告还在内存里，让用户换个位置存，不用重跑探查。"""
        from .probe import save_report
        msg = ("探查已经做完，但报告没能保存（多半是那个盘满了）。\n\n"
               "报告内容已经显示在下方日志里。现在选一个别的位置保存吗？（比如 C 盘桌面或 U 盘）")
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
            self._set_status("探查完成（报告已另存）", "ok")
            return

    def _worker(self, inp, out, mp):
        def confirm(text):
            ev = threading.Event()
            box = {}
            self.q.put(("confirm", (text, ev, box)))
            ev.wait()
            return box.get("ok", False)

        try:
            job = Job(inp, out, mp, workers=self._w, control=self.control, exclude_burned_in=self._exclude,
                      log=lambda m: self.q.put(("log", m)),
                      progress=lambda stage, done, total: self.q.put(("progress", (stage, done, total))),
                      confirm=confirm)
            ok = job.run()
            skipped = getattr(job, "stats", {}).get("skipped", 0)
        except Exception as e:  # 兜底，避免界面卡死
            self.q.put(("log", f"[错误] {_explain(e)}"))
            ok, skipped = False, 0
        self.q.put(("done", (ok, skipped)))

    def poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self._append_log(data)
                elif kind == "progress":
                    stage, done, total = data
                    self.progress.config(maximum=max(total, 1), value=done)
                    self.percent.config(text=f"{done * 100 // total}%" if total else "")
                    if not (self.control and (self.control.paused or self.control.stopped)):
                        self.status.config(text=f"{stage} {done}/{total}")
                elif kind == "confirm":
                    text, ev, box = data
                    preview = text if len(text) < 1500 else text[:1500] + "\n……（完整列表见下方日志）"
                    box["ok"] = messagebox.askyesno("确认", preview + "\n\n确认开始脱敏？")
                    ev.set()
                elif kind == "status":
                    if not (self.control and (self.control.paused or self.control.stopped)):
                        self.status.config(text=data)
                elif kind == "probe_save_again":
                    if not self.closing:
                        self._save_report_again(data)
                elif kind == "probe_done":
                    self._end()
                    self._set_status(*{True: ("探查完成", "ok"), False: ("探查出错", "error"),
                                       None: ("探查已停止", "warn")}[data])
                    if self.closing:
                        self.root.destroy()
                        return
                elif kind == "done":
                    ok, skipped = data
                    self._end()
                    if self.closing:
                        self.root.destroy()
                        return
                    if self.control.stopped:
                        self._set_status("已停止", "warn")
                        messagebox.showinfo("已停止", "已停止。已写出的文件都是完整的。\n\n"
                                                      "用同一个输出文件夹和映射表再点“开始脱敏”，会从停下的地方继续。")
                        continue
                    note = f"（{skipped} 个文件夹没有 DICOM，已跳过）" if skipped else ""
                    self._set_status(*(("全部完成，校验通过" + note, "ok") if ok
                                       else ("处理结束，有警告或错误，请看日志", "warn")))
                    if ok:
                        messagebox.showinfo("完成", "全部完成，校验通过。"
                                            + (f"\n\n另有 {skipped} 个文件夹里没有 DICOM 文件（比如只有图片），"
                                               "已跳过，日志里有记录。" if skipped else "")
                                            + "\n\n映射表和日志含患者信息，请勿外发。")
                    else:
                        messagebox.showwarning("完成", "处理结束，但有警告或错误，请查看日志。")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

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
        self.pause_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")
        self.status.config(text="正在停止，稍后自动关闭……")


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
