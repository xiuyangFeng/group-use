"""收集诊断信息，打成 zip，方便同事反馈问题（不联网、只读本地文件）。

包含：环境信息、config.json、gui.log 最后 200 行、audit.jsonl 最后 50 行、说明。
注意：日志里可能含文件路径和系统账户名，压缩包里会附一段提醒，发之前自己看一眼。
"""

from __future__ import annotations

import getpass
import os
import platform
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path


def _tail(path: Path, lines: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"（读取失败：{exc}）\n"
    parts = text.splitlines()
    return "\n".join(parts[-lines:]) + ("\n" if parts else "")


def _default_out_dir() -> Path:
    desktop = Path.home() / "Desktop"
    return desktop if desktop.is_dir() else Path.home()


def run(out_dir: str | None = None) -> int:
    from core.fsutil import platform_data_dir

    from . import __version__

    data_dir = platform_data_dir()
    target = Path(out_dir).expanduser() if out_dir else _default_out_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        target = Path(tempfile.gettempdir())
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = target / f"文件保险箱诊断-{ts}.zip"

    info = [
        f"文件保险箱 {__version__}",
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"系统：{platform.platform()}",
        f"Python：{sys.version.split()[0]}（打包版：{'是' if getattr(sys, 'frozen', False) else '否'}）",
        f"可执行文件：{sys.executable}",
        f"系统账户：{getpass.getuser()}",
        f"数据目录：{data_dir}",
        f"数据目录存在：{data_dir.is_dir()}",
    ]
    try:
        from core import native

        eng = native.info()
        if eng["engine"] == "C++":
            info.append(f"加密引擎：C++ {eng['version']}（{eng['impl']}，自检 {eng['selftest']}）")
        else:
            info.append(f"加密引擎：Python（C++ 引擎没加载：{eng.get('reason', '')}）")
    except Exception as exc:  # noqa: BLE001 —— 诊断包不能因为这个失败
        info.append(f"加密引擎：查询失败（{exc}）")
    for name, keys in [
        ("config.json", ["auto_lock_minutes", "minimize_to_tray", "version"]),
        ("audit.jsonl", []),
        ("gui.log", []),
    ]:
        p = data_dir / name
        info.append(f"{name}：{p}（{'存在' if p.exists() else '不存在'}）")

    note = (
        "这是「文件保险箱」诊断包。\n\n"
        "里面包含：环境信息、设置文件、最近的界面日志、最近的审计记录。\n"
        "可能含有：保险箱所在的文件夹路径、你的系统账户名、操作时间。\n"
        "**发出去之前请自己看一眼**；如果路径里有敏感信息，可以只发 gui.log 的最后 40 行。\n\n"
        "不含：密码、密保答案、保险箱里的文件内容。\n"
    )
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("说明.txt", note)
        zf.writestr("环境信息.txt", "\n".join(info) + "\n")
        if (data_dir / "config.json").exists():
            try:
                cfg = (data_dir / "config.json").read_text(encoding="utf-8", errors="replace")
                zf.writestr("config.json", cfg)
            except OSError:
                pass
        if (data_dir / "gui.log").exists():
            zf.writestr("gui.log", _tail(data_dir / "gui.log", 200))
        if (data_dir / "audit.jsonl").exists():
            zf.writestr("audit.jsonl", _tail(data_dir / "audit.jsonl", 50))
        for extra in ("验收结果.txt",):
            for base in (Path.cwd(), Path.home()):
                p = base / extra
                if p.exists():
                    zf.write(p, extra)
                    break

    print(f"诊断包已生成：{out}")
    if os.environ.get("FDVAULT_NO_OPEN") != "1":
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(out)])
            elif os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(out)])
        except OSError:
            pass
    return 0
