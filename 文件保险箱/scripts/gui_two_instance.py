"""真实两进程单实例检查（比在同一个进程里造两个 SingleInstance 更接近用户）。

用法：
    QT_QPA_PLATFORM=offscreen python scripts/gui_two_instance.py
预期：第一个实例保持运行；第二个实例应在 3 秒内退出（走“唤起已有窗口”分支）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("FDVAULT_NO_PROTECT", "1")  # 临时目录里的保险箱不加防删除保护，用完能直接删

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from core.console import setup_utf8_stdio  # noqa: E402


def main() -> int:
    setup_utf8_stdio()
    tmp = Path(tempfile.mkdtemp(prefix="fdvault-two-instance-"))
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(tmp),
            "USERPROFILE": str(tmp),
            "QT_QPA_PLATFORM": "offscreen",
            "PYTHONPATH": str(PROJECT),
            "FDVAULT_AUTO_QUIT_MS": "9000",
        }
    )
    exe = [sys.executable, str(PROJECT / "run_gui.py")]
    first = subprocess.Popen(exe, env=env, cwd=str(PROJECT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2.5)
        if first.poll() is not None:
            print(f"TWO-INSTANCE FAIL：第一个实例过早退出（exit {first.returncode}）")
            return 1
        started = time.time()
        second = subprocess.run(exe, env=env, cwd=str(PROJECT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        elapsed = time.time() - started
        if first.poll() is not None:
            print("TWO-INSTANCE FAIL：第二个实例启动后，第一个实例反而退出了")
            return 1
        if second.returncode != 0:
            print(f"TWO-INSTANCE FAIL：第二个实例退出码 {second.returncode}\n{second.stdout}\n{second.stderr}")
            return 1
        if elapsed >= 3:
            print(f"TWO-INSTANCE FAIL：第二个实例没有在 3 秒内退出（{elapsed:.1f}s），可能开了第二个窗口")
            print(second.stdout[-1000:])
            return 1
        print(f"TWO-INSTANCE PASS：第二个实例 {elapsed:.1f}s 内退出，第一个实例仍在运行")
        return 0
    finally:
        first.terminate()
        try:
            first.wait(timeout=10)
        except subprocess.TimeoutExpired:
            first.kill()
            first.wait()


if __name__ == "__main__":
    raise SystemExit(main())
