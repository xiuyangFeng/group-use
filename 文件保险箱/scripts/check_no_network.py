"""运行时“不联网”检查：启动程序，用 lsof 看进程有没有任何 IP 连接/监听。

静态检查（tests/test_no_network.py）保证代码里没有网络库；这个脚本补运行时证据。
macOS 用 lsof；其它平台打印跳过（Windows 由 CI 的静态检查 + 真机检查覆盖）。

用法：
    python scripts/check_no_network.py --binary "dist/文件保险箱.app/Contents/MacOS/文件保险箱"
    python scripts/check_no_network.py --source        # 用 run_gui.py 起源码版
    python scripts/check_no_network.py --binary ... --report reports/no-network.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from core.console import setup_utf8_stdio  # noqa: E402


def _lsof_ip(pid: int) -> str:
    proc = subprocess.run(
        ["lsof", "-nP", "-i", "-a", "-p", str(pid)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    # 第一行是表头；有数据行就说明有 IP 连接/监听
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return "\n".join(lines[1:]) if len(lines) > 1 else ""


def check_binary(binary: Path, report: Path | None = None, wait: float = 2.0) -> None:
    binary = Path(binary)
    if not binary.exists():
        raise SystemExit(f"找不到程序：{binary}")
    if sys.platform != "darwin":
        print("NO-NETWORK SKIP：非 macOS，运行时检查交给 Windows 真机/CI 静态检查")
        return
    fake_home = Path(tempfile.mkdtemp(prefix="fdvault-nonet-"))
    env = dict(os.environ)
    env.update({"HOME": str(fake_home), "USERPROFILE": str(fake_home), "QT_QPA_PLATFORM": "offscreen"})
    env["FDVAULT_AUTO_QUIT_MS"] = "6000"
    proc = subprocess.Popen([str(binary)], env=env, cwd=str(fake_home), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(wait)
        if proc.poll() is not None:
            raise SystemExit(f"程序启动后过早退出（exit {proc.returncode}），无法检查网络")
        sockets = _lsof_ip(proc.pid)
        ok = not sockets.strip()
        result = {"ok": ok, "pid": proc.pid, "sockets": sockets, "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        if report:
            report = Path(report)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"进程 PID {proc.pid} 的 IP 连接/监听：")
        print(sockets if sockets.strip() else "（无）")
        if not ok:
            raise SystemExit("FAIL：程序运行时有网络连接/监听")
        print("NO-NETWORK PASS：运行期间没有任何 IP 连接或监听")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def main() -> int:
    setup_utf8_stdio()
    parser = argparse.ArgumentParser(description="运行时无网络检查")
    parser.add_argument("--binary", default=None, help="打包后的可执行文件")
    parser.add_argument("--source", action="store_true", help="用源码版 run_gui.py 检查")
    parser.add_argument("--report", default=None, help="把结果写成 JSON")
    args = parser.parse_args()

    if args.source:
        binary = Path(__file__).resolve().parent.parent / "run_gui.py"
        # run_gui.py 是 python 脚本，需要解释器；这里用 python 起
        cmd = [sys.executable, str(binary)]
        if sys.platform != "darwin":
            print("NO-NETWORK SKIP：非 macOS")
            return 0
        fake_home = Path(tempfile.mkdtemp(prefix="fdvault-nonet-"))
        env = dict(os.environ)
        env.update({"HOME": str(fake_home), "QT_QPA_PLATFORM": "offscreen", "FDVAULT_AUTO_QUIT_MS": "6000"})
        proc = subprocess.Popen(cmd, env=env, cwd=str(fake_home), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(2.5)
            if proc.poll() is not None:
                raise SystemExit(f"源码版启动后过早退出（exit {proc.returncode}）")
            sockets = _lsof_ip(proc.pid)
            ok = not sockets.strip()
            if args.report:
                Path(args.report).write_text(
                    json.dumps({"ok": ok, "pid": proc.pid, "sockets": sockets}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            print(sockets if sockets.strip() else "（无）")
            print("NO-NETWORK PASS：运行期间没有任何 IP 连接或监听" if ok else "FAIL：检测到网络连接")
            return 0 if ok else 1
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

    if not args.binary:
        # 自动找 dist 里的程序
        app = Path(__file__).resolve().parent.parent / "dist" / "文件保险箱.app" / "Contents" / "MacOS" / "文件保险箱"
        if not app.exists():
            raise SystemExit("请用 --binary 指定程序，或先打包")
        args.binary = str(app)
    check_binary(Path(args.binary), Path(args.report) if args.report else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
