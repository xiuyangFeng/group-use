"""处理记录：每次脱敏 / 结构探查的概况，存在本机固定位置，下次打开软件可以查看。

- 只记录时间、路径（原始数据 / 输出 / 映射表 / 日志 / 报告）、数量和结果，
  不记录患者姓名、ID、患者文件夹名。每个患者文件夹的明细在映射表里（映射表只留在本地）。
- 文件只追加，从不修改、不删除。写记录失败不影响脱敏本身。
- 位置：Windows %APPDATA%\\CT_Deid\\处理记录.jsonl；Mac ~/Library/Application Support/CT_Deid/
"""

import json
import os
import secrets
import subprocess
import sys
from datetime import datetime
from pathlib import Path

FILE_NAME = "处理记录.jsonl"


def history_dir():
    if os.environ.get("DEID_HOME"):  # 测试用
        return Path(os.environ["DEID_HOME"])
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "CT_Deid"


def history_path():
    return history_dir() / FILE_NAME


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_run_id():
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def record(run_id, event, **fields):
    """追加一行记录。失败时返回错误信息（调用方写进日志），不抛异常。"""
    line = {"id": run_id, "event": event, "time": now(), **fields}
    try:
        path = history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+b") as f:
            f.seek(0, os.SEEK_END)
            if f.tell():
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    f.write(b"\n")  # 上次写到一半（断电等）留下半行，另起一行，不和它粘在一起
            f.write((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError as e:
        return f"处理记录写入失败（不影响脱敏）：{e}"
    return None


def load_runs():
    """返回所有运行记录，最新的在前。没有结束记录的标为“未正常结束”。"""
    try:
        text = history_path().read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []
    runs = {}
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue  # 写到一半断电留下的半行
        rid = ev.get("id")
        if not rid:
            continue
        r = runs.setdefault(rid, {"id": rid})
        kind = ev.pop("event", "")
        t = ev.pop("time", "")
        r.update(ev)
        if kind == "start":
            r["start"] = t
        elif kind == "end":
            r["end"] = t
    for r in runs.values():
        if "end" not in r:
            r["result"] = "未正常结束（程序被关闭或意外退出）"
    return list(reversed(runs.values()))  # 文件按时间追加，第一次出现的顺序就是开始的先后


def last_deid_run():
    return next((r for r in load_runs() if r.get("kind") == "脱敏"), None)


def summarize(r):
    """一次运行的多行文字说明（界面和命令行共用）。"""
    lines = [f"{r.get('kind', '')}  {r.get('start', '?')} → {r.get('end', '未结束')}",
             f"结果：{r.get('result', '')}"]
    if r.get("kind") == "脱敏":
        lines += [f"原始数据：{r.get('input', '')}",
                  f"输出：{r.get('output', '')}",
                  f"映射表：{r.get('mapping', '')}"]
        if "folders" in r:
            lines.append(f"{r['folders']} 个文件夹 → {r.get('patients', 0)} 个患者编号"
                         + (f"，{r['skipped']} 个文件夹跳过（没有 DICOM）" if r.get("skipped") else ""))
        if "written" in r:
            lines.append(f"共 {r.get('files_total', 0)} 个文件：本次写出 {r['written']}，"
                         f"之前已完成 {r.get('existing', 0)}")
        if r.get("status_counts"):
            lines.append("各患者：" + "，".join(f"{k} {v}" for k, v in r["status_counts"].items())
                         + (f"，未开始 {r['not_started']}" if r.get("not_started") else ""))
    else:
        lines.append(f"原始数据：{r.get('input', '')}")
        if r.get("report"):
            lines.append(f"报告：{r['report']}")
    if r.get("log"):
        lines.append(f"日志：{r['log']}")
    return "\n".join(lines)


def _norm(p):
    return os.path.normcase(os.path.normpath(str(p)))


def folder_status(input_dir, mapping_path):
    """原始数据文件夹里每个子文件夹最近一次的处理情况（从映射表读取）。

    返回 [(文件夹名, 映射表行 或 None)]；None 表示映射表里没有这个文件夹的记录。
    """
    from .mapping import read_rows
    rows = read_rows(mapping_path) if Path(mapping_path).exists() else []
    key = _norm(Path(input_dir).resolve())
    latest = {}
    for r in rows:  # 映射表按时间追加，后面的行覆盖前面的
        src = str(r.get("原始数据文件夹") or "")
        if src and _norm(src) != key:
            continue  # 别的原始数据文件夹里的同名文件夹
        latest[str(r.get("原文件夹") or "")] = r
    folders = sorted(p.name for p in Path(input_dir).iterdir() if p.is_dir() and not p.name.startswith("."))
    return [(name, latest.get(name)) for name in folders]


def open_path(path):
    """用系统默认程序打开文件或文件夹（只打开，不做任何修改）。"""
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
