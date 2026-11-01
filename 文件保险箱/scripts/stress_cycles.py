"""验收第 12 条：连续 100 次解锁/上锁，磁盘上不能找到任何明文残留。

做法：每个文件里放一个唯一的随机标记字符串（比如 SECRET-3f2a...），
每次解锁后确认标记都在；每次上锁后翻遍整个保险箱目录（包括 .vault），
确认标记一个都搜不到、敏感文件名也搜不到。

用法：
    FDVAULT_KDF_FAST=1 .venv/bin/python scripts/stress_cycles.py [--cycles 100]
结果写到 reports/stress-cycles.json。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from core.console import setup_utf8_stdio
from core import constants as C  # noqa: E402
from core.vault import Vault  # noqa: E402

PASSWORD = "stress-cycles-password-1234"


def make_files(root: Path) -> list[str]:
    root.mkdir(parents=True, exist_ok=True)
    markers = []
    names = [
        "患者姓名-张三.txt",
        "伦理审查-原始记录.txt",
        "sub/病历 2024-01.txt",
        "sub/deep/图片说明.txt",
        "空文件.txt",
    ]
    for i, name in enumerate(names):
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith("空文件.txt"):
            p.write_bytes(b"")
            continue
        token = f"SECRET-{secrets.token_hex(8)}-{i}"
        markers.append(token)
        p.write_text(f"这是敏感内容。\n标记：{token}\n", encoding="utf-8")
    (root / "空目录" / "子目录").mkdir(parents=True, exist_ok=True)
    return markers + [n for n in names if not n.endswith("空文件.txt")]


def search_plaintext(root: Path, needles: list[bytes]) -> list[str]:
    """翻遍整个保险箱目录，返回搜到的明文标记/文件名。"""
    hits: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                data = p.read_bytes()
            except OSError:
                continue
            for needle in needles:
                if needle in data:
                    hits.append(f"{p.relative_to(root)} 含 {needle[:40]!r}")
    return hits


def main() -> int:
    setup_utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=100)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="fdvault-cycles-"))
    root = tmp / "患者数据"
    markers = make_files(root)
    token_bytes = [(m.split("：")[1] if "：" in m else m).encode() for m in markers if m.startswith("SECRET-")]
    extra_names = [m.encode() for m in markers if not m.startswith("SECRET-")]

    v = Vault(root, audit_dir=tmp / "audit")
    v.create(PASSWORD)
    v.close()

    report = {
        "cycles": args.cycles,
        "files": len(markers),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rounds": [],
        "failures": [],
    }
    started = time.monotonic()
    for i in range(1, args.cycles + 1):
        try:
            v = Vault(root, audit_dir=tmp / "audit")
            v.unlock(PASSWORD)
            v.close()
            for needle in token_bytes:
                found = search_plaintext(root, [needle])
                if not found:
                    report["failures"].append({"round": i, "when": "unlock", "missing": needle.decode()})
            # 上锁
            v = Vault(root, audit_dir=tmp / "audit")
            v.lock(PASSWORD)
            v.close()
            hits = search_plaintext(root, token_bytes + extra_names)
            if hits:
                report["failures"].append({"round": i, "when": "lock", "hits": hits[:10]})
            if Vault(root).status()["state"] != C.LOCKED:
                report["failures"].append({"round": i, "when": "lock", "state": "not LOCKED"})
        except Exception as exc:  # noqa: BLE001
            report["failures"].append({"round": i, "when": "exception", "error": f"{type(exc).__name__}: {exc}"})
        if i % 10 == 0 or i == args.cycles:
            print(f"  {i}/{args.cycles} 轮完成，用时 {time.monotonic() - started:.1f}s", flush=True)

    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    report["failures"] = report["failures"][:50]
    report["ok"] = not report["failures"]
    (PROJECT / "reports").mkdir(exist_ok=True)
    out = PROJECT / "reports" / "stress-cycles.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{'通过' if report['ok'] else '失败'}：{args.cycles} 轮，用时 {report['elapsed_seconds']}s，失败 {len(report['failures'])} 项")
    if report["failures"]:
        print(json.dumps(report["failures"][:5], ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
