#!/usr/bin/env python3
"""断电压力测试：反复在随机位置强杀进程，恢复后逐字节比对明文树。

用法：
    .venv/bin/python scripts/stress_crash.py --rounds 60 --seed 20261101

每轮会做两次随机断电（解锁途中一次、上锁途中一次），恢复方式随机（继续/回滚），
然后比对整棵明文树（内容 sha256 + mtime + 权限位），并检查上锁后没有明文残留。

退出码 0 表示全部通过；非 0 表示有数据不一致（会打印明细）。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("FDVAULT_NO_PROTECT", "1")  # 临时目录里的保险箱不加防删除保护，用完能直接删

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from tests.conftest import PASSWORD, make_tree, snapshot  # noqa: E402  (需要先设置 sys.path)
from core.console import setup_utf8_stdio  # noqa: E402

MARKER = b"STRESS-PLAINTEXT-MARKER-DO-NOT-LEAK"
MARKER_NAME = "标记-不要泄漏.txt"

UNLOCK_POINTS = [
    ("unlock:after_blob_read_verify", 7),
    ("unlock:before_rename", 7),
    ("unlock:after_rename", 7),
    ("unlock:before_manifest_commit", 1),
    ("unlock:after_manifest_commit", 1),
    ("unlock:before_unlink_blob", 7),
    ("unlock:after_unlink_blob", 7),
]

LOCK_POINTS = [
    ("lock:before_blob_rename", 7),
    ("lock:after_blob_rename", 7),
    ("lock:before_manifest_commit", 1),
    ("lock:after_manifest_commit", 1),
    ("lock:before_unlink_plaintext", 7),
    ("lock:after_unlink_plaintext", 7),
]

STATE_LOCKED = "LOCKED"
STATE_UNLOCKED = "UNLOCKED"


class Runner:
    def __init__(self, root: Path, audit_dir: Path, python: Path):
        self.root = root
        self.audit_dir = audit_dir
        self.python = python
        self.runs = 0

    def run(self, args: list[str], *, stdin: str = PASSWORD + "\n", crash_at: str | None = None):
        env = {
            "PYTHONPATH": str(PROJECT),
            "FDVAULT_KDF_FAST": "1",
            "FDVAULT_CHUNK_SIZE": "65536",
        }
        if crash_at:
            env["FDVAULT_CRASH_AT"] = crash_at
        cmd = [str(self.python), "-m", "cli.main", *args, "--audit-dir", str(self.audit_dir)]
        self.runs += 1
        return subprocess.run(
            cmd, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=str(PROJECT), timeout=600
        )

    def state(self) -> str:
        from core.vault import Vault

        return Vault(self.root).status()["state"]

    def unlock(self, crash_at: str | None = None):
        return self.run(["unlock", str(self.root), "--password-stdin", "--no-progress"], crash_at=crash_at)

    def lock(self, crash_at: str | None = None):
        return self.run(["lock", str(self.root), "--password-stdin", "--no-progress"], crash_at=crash_at)

    def recover(self, rollback: bool, crash_at: str | None = None):
        args = ["recover", str(self.root), "--password-stdin", "--no-progress", "--yes"]
        if rollback:
            args.append("--rollback")
        return self.run(args, crash_at=crash_at)


def check_no_residue(root: Path) -> list[str]:
    problems: list[str] = []
    allowed = {".vault", "如何解锁.txt"}
    names = {p.name for p in root.iterdir()}
    extra = names - allowed
    if extra:
        problems.append(f"上锁后文件夹里仍有明文条目：{sorted(extra)}")
    for p in (root / ".vault").rglob("*"):
        if not p.is_file():
            continue
        data = p.read_bytes()
        if MARKER in data:
            problems.append(f"{p.relative_to(root)} 里出现明文内容")
        if MARKER_NAME.encode() in data:
            problems.append(f"{p.relative_to(root)} 里出现明文文件名")
    return problems


def main(argv: list[str] | None = None) -> int:
    setup_utf8_stdio()
    ap = argparse.ArgumentParser(description="断电压力测试")
    ap.add_argument("--rounds", type=int, default=60, help="轮数（每轮 2 次断电）")
    ap.add_argument("--seed", type=int, default=20261101)
    ap.add_argument("--nested", type=float, default=0.2, help="恢复过程中再次断电的概率")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--report", default=None, help="JSON 结果输出路径")
    args = ap.parse_args(argv)

    python = Path(sys.executable)
    workdir = Path(args.workdir) if args.workdir else PROJECT / ".stress"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    audit_dir = workdir / "audit"
    audit_dir.mkdir()
    root = workdir / "data"
    make_tree(root)
    (root / MARKER_NAME).write_bytes(MARKER)
    expected = snapshot(root)

    rng = random.Random(args.seed)
    runner = Runner(root, audit_dir, python)

    r = runner.run(["create", str(root), "--password-stdin", "--yes", "--no-progress"])
    assert r.returncode == 0, r.stderr
    assert runner.state() == STATE_LOCKED

    stats = {
        "rounds": args.rounds,
        "kills": 0,
        "skipped_no_hit": 0,
        "nested_kills": 0,
        "mismatches": [],
        "residue": [],
        "final_ok": False,
    }
    t0 = time.monotonic()

    def ensure_locked():
        if runner.state() == STATE_UNLOCKED:
            rr = runner.lock()
            assert rr.returncode == 0, rr.stderr
        assert runner.state() == STATE_LOCKED

    def ensure_unlocked():
        if runner.state() == STATE_LOCKED:
            rr = runner.unlock()
            assert rr.returncode == 0, rr.stderr
        assert runner.state() == STATE_UNLOCKED

    def crash_and_recover(direction: str):
        """返回 True 表示真的命中了断电点。"""
        points = UNLOCK_POINTS if direction == "unlock" else LOCK_POINTS
        point, maxcount = rng.choice(points)
        count = rng.randint(1, maxcount)
        action = runner.unlock if direction == "unlock" else runner.lock
        r = action(crash_at=f"{point}={count}")
        if r.returncode != 70:
            # 注入点没命中（例如 count 超出实际次数），本轮跳过恢复
            stats["skipped_no_hit"] += 1
            return False
        stats["kills"] += 1
        in_progress = runner.state()
        assert in_progress in ("UNLOCKING", "LOCKING"), in_progress

        rollback = rng.random() < 0.3
        if args.nested and rng.random() < args.nested:
            nested_point, nested_max = rng.choice(UNLOCK_POINTS + LOCK_POINTS)
            rr = runner.recover(rollback=rollback, crash_at=f"{nested_point}={rng.randint(1, nested_max)}")
            if rr.returncode == 70:
                stats["nested_kills"] += 1
        rr = runner.recover(rollback=rollback)
        assert rr.returncode == 0, rr.stderr
        assert runner.state() in (STATE_LOCKED, STATE_UNLOCKED), runner.state()
        return True

    for i in range(1, args.rounds + 1):
        ensure_locked()
        crash_and_recover("unlock")
        # 校验明文（若恢复结果仍是 LOCKED，就正常解锁再比）
        if runner.state() == STATE_LOCKED:
            ensure_unlocked()
        if snapshot(root) != expected:
            stats["mismatches"].append({"round": i, "when": "after_unlock_recover"})

        crash_and_recover("lock")
        if runner.state() == STATE_UNLOCKED:
            if snapshot(root) != expected:
                stats["mismatches"].append({"round": i, "when": "after_lock_recover"})
            ensure_locked()
        else:
            ensure_locked()
            ensure_unlocked()
            if snapshot(root) != expected:
                stats["mismatches"].append({"round": i, "when": "after_lock_recover_locked"})
            ensure_locked()

        stats["residue"].extend([f"round {i}: {p}" for p in check_no_residue(root)])
        if i % 10 == 0 or i == args.rounds:
            print(
                f"  第 {i}/{args.rounds} 轮：累计 {stats['kills']} 次断电，"
                f"{len(stats['mismatches'])} 处不一致，{len(stats['residue'])} 处残留",
                flush=True,
            )

    ensure_locked()
    # 上锁状态下明文树必须为空（说明文件除外）
    if snapshot(root) != {}:
        stats["mismatches"].append({"round": "final", "when": "locked_tree_not_empty"})
    stats["residue"].extend([f"final: {p}" for p in check_no_residue(root)])

    ensure_unlocked()
    if snapshot(root) != expected:
        stats["mismatches"].append({"round": "final", "when": "final_unlock_snapshot"})
    stats["final_ok"] = snapshot(root) == expected
    stats["elapsed_sec"] = round(time.monotonic() - t0, 1)
    stats["cli_invocations"] = runner.runs

    ok = not stats["mismatches"] and not stats["residue"] and stats["final_ok"]
    print()
    print("=" * 60)
    print(f"轮数：{stats['rounds']}    命中断电：{stats['kills']} 次    "
          f"恢复中再次断电：{stats['nested_kills']} 次")
    print(f"不一致：{len(stats['mismatches'])}    明文残留：{len(stats['residue'])}    "
          f"耗时：{stats['elapsed_sec']} 秒")
    print("结论：" + ("✅ 全部通过，0 字节丢失" if ok else "❌ 发现问题"))
    print("=" * 60)

    if args.report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入：{out}")

    shutil.rmtree(workdir, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
