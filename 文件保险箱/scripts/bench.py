#!/usr/bin/env python3
"""性能基准：验证「1 万个小文件 / 10 GB，上锁+解锁各 5 分钟内」这类目标。

用法：
    .venv/bin/python scripts/bench.py --files 10000 --file-size 200000
    .venv/bin/python scripts/bench.py --files 1 --file-size 2000000000

会打印每个阶段耗时、吞吐量，并给出线性外推结果。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("FDVAULT_NO_PROTECT", "1")  # 临时目录里的保险箱不加防删除保护，用完能直接删

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from core.console import setup_utf8_stdio
from core.fsutil import human_bytes  # noqa: E402
from core.vault import Vault  # noqa: E402

PASSWORD = "Bench-Password-1234"


def make_files(root: Path, count: int, size: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = os.urandom(min(size, 1024 * 1024))
    for i in range(count):
        p = root / f"组{i // 1000:03d}" / f"文件{i:05d}.dat"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            remaining = size
            while remaining > 0:
                n = min(remaining, len(payload))
                f.write(payload[:n])
                remaining -= n


def timed(fn):
    t0 = time.monotonic()
    result = fn()
    return round(time.monotonic() - t0, 2), result


def main(argv: list[str] | None = None) -> int:
    setup_utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=10000)
    ap.add_argument("--file-size", type=int, default=200_000, help="每个文件字节数")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--report", default=None)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)

    workdir = Path(args.workdir) if args.workdir else PROJECT / ".bench"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    root = workdir / "data"
    audit_dir = workdir / "audit"
    audit_dir.mkdir()

    total_bytes = args.files * args.file_size
    print(f"准备数据：{args.files} 个文件 × {human_bytes(args.file_size)} = {human_bytes(total_bytes)}")
    t, _ = timed(lambda: make_files(root, args.files, args.file_size))
    print(f"  写入数据：{t} 秒")

    v = Vault(root, audit_dir=audit_dir)
    os.environ.setdefault("FDVAULT_CHUNK_SIZE", "65536")
    t_create, res = timed(lambda: v.create(PASSWORD))
    v.close()
    print(f"  创建保险箱（含首次上锁）：{t_create} 秒（{human_bytes(total_bytes / max(t_create, 0.01))}/s）")

    v = Vault(root, audit_dir=audit_dir)
    t_unlock, _ = timed(lambda: v.unlock(password=PASSWORD))
    t_lock, _ = timed(lambda: v.lock(password=PASSWORD))
    v.close()

    v = Vault(root, audit_dir=audit_dir)
    t_verify, verify_res = timed(lambda: v.verify(password=PASSWORD))
    v.close()

    report = {
        "files": args.files,
        "file_size": args.file_size,
        "total_bytes": total_bytes,
        "create_sec": t_create,
        "unlock_sec": t_unlock,
        "lock_sec": t_lock,
        "verify_sec": t_verify,
        "lock_bytes_per_sec": int(total_bytes / max(t_lock, 0.01)),
        "unlock_bytes_per_sec": int(total_bytes / max(t_unlock, 0.01)),
        "verify_ok": verify_res["ok"],
        "target_5min": max(t_lock, t_unlock) < 300,
    }
    # 目标场景外推：1 万文件 / 10 GB
    if args.files and args.file_size:
        target_bytes = 10 * 1000 * 1000 * 1000
        scale = target_bytes / (args.files * args.file_size)
        report["extrapolated_10k_10gb_sec"] = round(max(t_lock, t_unlock) * scale, 1)

    print()
    print(f"  解锁：{t_unlock} 秒（{human_bytes(report['unlock_bytes_per_sec'])}/s）")
    print(f"  上锁：{t_lock} 秒（{human_bytes(report['lock_bytes_per_sec'])}/s）")
    print(f"  校验：{t_verify} 秒，结果 {'通过' if verify_res['ok'] else '失败'}")
    print(f"  5 分钟目标（单次 ≤300 秒）：{'✅ 达标' if report['target_5min'] else '❌ 未达标'}")
    print(f"  线性外推到 1 万文件 / 10 GB：约 {report.get('extrapolated_10k_10gb_sec')} 秒/次")

    if args.report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  报告：{out}")
    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0 if report["target_5min"] and verify_res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
