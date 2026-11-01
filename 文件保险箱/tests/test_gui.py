"""GUI 自检：Qt 的退出析构对 pytest 进程不友好，真正的窗口检查在子进程里跑。

子进程脚本：scripts/gui_selftest.py（离屏渲染，不弹窗、不依赖屏幕）。
这个文件只保留一个子进程调用和几个纯配置的测试，保证 pytest 本身干净退出。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def gui_selftest_result() -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        {
            "QT_QPA_PLATFORM": "offscreen",
            "FDVAULT_KDF_FAST": "1",
            "PYTHONPATH": str(PROJECT),
        }
    )
    return subprocess.run(
        [sys.executable, str(PROJECT / "scripts" / "gui_selftest.py")],
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
        cwd=str(PROJECT),
    )


def test_gui_selftest(gui_selftest_result):
    proc = gui_selftest_result
    assert proc.returncode == 0, (
        f"GUI 自检失败（exit {proc.returncode}）\n"
        f"STDOUT:\n{proc.stdout[-5000:]}\nSTDERR:\n{proc.stderr[-5000:]}"
    )
    assert "GUI SELFTEST PASS" in proc.stdout


def test_exit_auto_lock_subprocess():
    """验收第 11 条：退出前自动上锁（独立子进程，避免 Qt 退出析构问题）。"""
    env = dict(os.environ)
    env.update({"QT_QPA_PLATFORM": "offscreen", "FDVAULT_KDF_FAST": "1", "PYTHONPATH": str(PROJECT)})
    proc = subprocess.run(
        [sys.executable, str(PROJECT / "scripts" / "gui_exit_lock.py")],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=str(PROJECT),
    )
    assert proc.returncode == 0, (
        f"退出自动上锁失败（exit {proc.returncode}）\n"
        f"STDOUT:\n{proc.stdout[-5000:]}\nSTDERR:\n{proc.stderr[-5000:]}"
    )
    assert "EXITLOCK PASS" in proc.stdout


def test_report_file_output(tmp_path):
    """--report 写文件：Windows --windowed 没有 stdout，验收脚本靠这个文件判断结果。"""
    env = dict(os.environ)
    env.update({"QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(PROJECT)})
    report = tmp_path / "selftest-report.txt"
    proc = subprocess.run(
        [sys.executable, str(PROJECT / "run_gui.py"), "--selftest", "--report", str(report)],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=str(PROJECT),
    )
    assert proc.returncode == 0, proc.stderr
    assert report.exists()
    assert "SELFCHECK PASS" in report.read_text(encoding="utf-8")


def test_acceptance_subprocess():
    """打包/源码的 --acceptance：真实参数端到端（测试里用快 KDF 缩短时间）。"""
    env = dict(os.environ)
    env.update(
        {
            "QT_QPA_PLATFORM": "offscreen",
            "FDVAULT_KDF_FAST": "1",
            "PYTHONPATH": str(PROJECT),
        }
    )
    report = Path(tempfile.mkdtemp()) / "acceptance-report.txt"
    proc = subprocess.run(
        [sys.executable, str(PROJECT / "run_gui.py"), "--acceptance", "--report", str(report)],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        cwd=str(PROJECT),
    )
    assert proc.returncode == 0, (
        f"真实参数验收失败（exit {proc.returncode}）\nSTDOUT:\n{proc.stdout[-5000:]}\nSTDERR:\n{proc.stderr[-5000:]}"
    )
    assert report.exists() and "ACCEPTANCE PASS" in report.read_text(encoding="utf-8")


def test_gui_version_flag():
    """安装包里 `文件保险箱 --version` 应直接打印版本号、不打开窗口。"""
    env = dict(os.environ)
    env.update({"QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(PROJECT)})
    proc = subprocess.run(
        [sys.executable, str(PROJECT / "run_gui.py"), "--version"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(PROJECT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "文件保险箱 0.3.0" in proc.stdout, proc.stdout


def test_store_config_roundtrip(tmp_path):
    from gui.store import AppStore

    s = AppStore(tmp_path / "cfg")
    s.add_vault(tmp_path / "a")
    s.add_vault(tmp_path / "b")
    s.set_auto_lock(30)
    s2 = AppStore(tmp_path / "cfg")
    assert {Path(p).name for p in s2.vault_paths()} == {"a", "b"}
    assert s2.auto_lock_minutes == 30
    s2.remove_vault(str(tmp_path / "a"))
    assert len(s2.vault_paths()) == 1


def test_idle_seconds_returns_value():
    from gui.store import idle_seconds

    value = idle_seconds()
    assert value is None or value >= 0
