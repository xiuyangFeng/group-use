"""生成 dist/交付清单.md：产物、SHA256、文档、测试证据、待办一页看全。

用法（项目根目录）：
    .venv/bin/python scripts/delivery_manifest.py
打包脚本会在最后自动调用。清单本身不含敏感信息，只列文件名和数字。
"""

from __future__ import annotations

import ast
import hashlib
import json
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

DIST = PROJECT / "dist"
REPORTS = PROJECT / "reports"

DOC_ITEMS = [
    ("README.md", "项目入口、快速开始、开发说明", "已完成"),
    ("docs/需求说明书.md", "需求与边界（v0.2）", "已确认"),
    ("docs/技术方案.md", "架构、密钥/文件格式、状态机、恢复、审计、打包", "已完成"),
    ("docs/测试报告-M1.md", "加密核心 + CLI + 断电/崩溃", "已完成"),
    ("docs/测试报告-M2.md", "macOS GUI + 托盘 + 自动上锁 + 恢复向导 + 审计", "已完成"),
    ("docs/测试报告-M3.md", "Windows 打包 + GitHub Actions + 安装包验收", "部分：Windows 真机待联调"),
    ("docs/测试报告-M4.md", "快速操作 + 真实数据试点 + 验收清单 + 使用文档", "部分：试点/真机待完成"),
    ("docs/使用说明.md", "给组里同事的使用手册", "已完成"),
    ("installers/macos/安装说明.md", "macOS 安装、首次打开、快速操作", "已完成"),
    ("installers/windows/安装说明.md", "Windows 安装、右键菜单、验收自检、自己构建", "已完成"),
    ("docs/验收清单.md", "需求 13 条逐条结果", "10 ✅ + 3 ☑ 待真机"),
    ("docs/验收记录表.md", "打印/签字用的验收记录", "模板已就绪"),
    ("docs/Windows真机联调记录.md", "22 步 Windows 真机检查单", "待真机执行"),
    ("docs/macOS真机检查记录.md", "19 步 macOS 真机检查单", "待真机执行"),
    ("docs/试点方案.md", "脱敏数据试点 7 天方案", "待执行"),
    ("docs/发布说明-0.3.1.md", "这一版能做什么、限制、验证概况", "已完成"),
]

MANUAL_TODO = [
    "Windows 共用电脑：跑 build_windows.ps1 或解压安装包，执行验收自检.ps1 + 22 步联调记录",
    "macOS 真机：菜单栏图标、5 分钟自动上锁、快速操作、第二个账户隔离",
    "多系统账户隔离：第二个账户访问第一个账户的保险箱应被拒绝",
    "脱敏数据试点：按试点方案走 7 天并填验收记录表",
    "最终产品名字：确认后统一改安装包/快捷方式/文档标题",
    "是否推 GitHub：确认后启用 Actions 自动构建 macOS/Windows 产物",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _size_mb(path: Path) -> str:
    return f"{path.stat().st_size / 1024 / 1024:.1f} MB"


def count_tests(root: Path) -> int:
    total = 0
    for p in (root / "tests").rglob("test_*.py"):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        total += sum(
            1 for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
        )
    return total


def collected_tests(root: Path) -> int:
    """优先用 pytest 实际收集数（参数化会一个函数生成多个用例）。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "--collect-only"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        for line in proc.stdout.splitlines():
            m = re.search(r"(\d+) tests? collected", line)
            if m:
                return int(m.group(1))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return count_tests(root)


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _evidence(root: Path) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    rp = root / "reports"
    bench = _json(rp / "bench-10k-10gb.json")
    if bench:
        rows.append(
            (
                "性能：1 万文件 / 9.3 GB（默认分块）",
                f"解锁 {bench.get('unlock_sec')} s、上锁 {bench.get('lock_sec')} s、校验 {bench.get('verify_sec')} s",
                "reports/bench-10k-10gb.json",
            )
        )
    crash = _json(rp / "stress-crash-120kills.json")
    if crash:
        rows.append(
            (
                "断电恢复：随机强杀",
                f"{crash.get('rounds')} 轮 / {crash.get('kills')} 次杀进程 / 嵌套杀 {crash.get('nested_kills')} 次；"
                f"不一致 {len(crash.get('mismatches', []))}、明文残留 {len(crash.get('residue', []))}",
                "reports/stress-crash-120kills.json",
            )
        )
    nonet = _json(rp / "no-network-runtime.json")
    if nonet:
        rows.append(
            (
                "运行时无网络（lsof 实测）",
                f"IP 连接/监听 {0 if nonet.get('ok') else '有'} 个；检查时间 {nonet.get('checked_at')}",
                "reports/no-network-runtime.json",
            )
        )
    pilot = _json(rp / "pilot-rehearsal.json")
    if pilot:
        steps = pilot.get("steps", [])
        rows.append(
            (
                "试点流程彩排（假数据）",
                f"{len(steps)} 步、失败 {sum(1 for s in steps if not s.get('ok'))}、耗时 {pilot.get('seconds')} s",
                "reports/pilot-rehearsal.json",
            )
        )
    cycles = _json(rp / "stress-cycles.json")
    if cycles:
        rows.append(
            (
                "100 次解锁/上锁残留扫描",
                f"{cycles.get('cycles')} 轮、失败 {len(cycles.get('failures', []))}、耗时 {cycles.get('elapsed_seconds')} s",
                "reports/stress-cycles.json",
            )
        )
    return rows


def _acceptance_summary(root: Path) -> str:
    """只统计表格行（图例里的 ✅/☑/⏳ 不算）。"""
    p = root / "docs" / "验收清单.md"
    if not p.exists():
        return "（未找到验收清单）"
    text = p.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if re.match(r"^\|\s*\d+\s*\|", line)]
    counts = {k: sum(1 for line in rows for _ in [0] if k in line) for k in ("✅", "☑", "⏳")}
    return f"通过 ✅ {counts['✅']} 项、真机待确认 ☑ {counts['☑']} 项、未开始 ⏳ {counts['⏳']} 项"


def build_manifest(root: Path = PROJECT) -> str:
    root = Path(root)
    dist = root / "dist"
    from gui import __version__

    now = time.strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []
    lines.append(f"# 文件保险箱 {__version__} 交付清单")
    lines.append("")
    lines.append(f"生成时间：{now} · 平台：{platform.platform()} · Python {sys.version.split()[0]}")
    lines.append("")
    lines.append("## 1. 安装包（dist/）")
    lines.append("")
    lines.append("| 文件 | 大小 | SHA256 |")
    lines.append("|---|---|---|")
    files = sorted([p for p in dist.glob("文件保险箱-*") if p.suffix in (".zip", ".dmg")] + [dist / "SHA256SUMS.txt"])
    for p in files:
        if not p.exists():
            continue
        lines.append(f"| `{p.name}` | {_size_mb(p)} | `{sha256(p)}` |")
    lines.append("")
    lines.append("校验：`cd dist && shasum -a 256 -c SHA256SUMS.txt`（Windows 用 `Get-FileHash`）。")
    lines.append("")
    lines.append("## 2. 文档")
    lines.append("")
    lines.append("| 文件 | 内容 | 状态 |")
    lines.append("|---|---|---|")
    for rel, desc, status in DOC_ITEMS:
        lines.append(f"| [{rel}](../{rel}) | {desc} | {status} |")
    lines.append("")
    lines.append("## 3. 自动化测试与证据")
    lines.append("")
    lines.append(f"- 测试：pytest 实际收集 {collected_tests(root)} 项（macOS 上 3 项 Windows 专用跳过）")
    lines.append(f"- 验收清单：{_acceptance_summary(root)}")
    lines.append("")
    ev = _evidence(root)
    if ev:
        lines.append("| 证据 | 结果 | 文件 |")
        lines.append("|---|---|---|")
        for name, result, rel in ev:
            lines.append(f"| {name} | {result} | `{rel}` |")
        lines.append("")
    lines.append("## 4. 安装包内置检查")
    lines.append("")
    lines.append("```")
    lines.append("文件保险箱 --version                  # 版本号")
    lines.append("文件保险箱 --selftest --report 报告.txt   # 组件自检 6 项")
    lines.append("文件保险箱 --acceptance --report 报告.txt # 默认参数端到端 6 项")
    lines.append("文件保险箱 --diagnostics --out 桌面      # 出问题收集日志（不含密码）")
    lines.append("```")
    lines.append("")
    lines.append("## 5. 还不能自动验证、需要真人/真机的事")
    lines.append("")
    for item in MANUAL_TODO:
        lines.append(f"- [ ] {item}")
    lines.append("")
    lines.append("## 6. 复现方式")
    lines.append("")
    lines.append("```bash")
    lines.append("python -m pip install -r requirements.txt -r requirements-dev.txt")
    lines.append("python -m pytest tests -q")
    lines.append("python scripts/gui_selftest.py")
    lines.append("python scripts/build_app.py")
    lines.append("```")
    lines.append("")
    lines.append("*清单由 scripts/delivery_manifest.py 生成；安装包改动后请重新运行打包脚本。*")
    return "\n".join(lines) + "\n"


def write_manifest(root: Path = PROJECT) -> Path:
    root = Path(root)
    out = root / "dist" / "交付清单.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_manifest(root), encoding="utf-8")
    return out


if __name__ == "__main__":
    from core.console import setup_utf8_stdio

    setup_utf8_stdio()
    p = write_manifest()
    print(f"已生成：{p}")
