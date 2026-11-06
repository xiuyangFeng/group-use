"""测试夹具与工具函数。"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

# GUI 测试一律走离屏渲染：不弹窗、不依赖屏幕，CI 也能跑
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PASSWORD = "246810"
# 新建保险箱必须带 3 个密保问题；答对任意一个即可
QUESTIONS = [("我的生日", "1998-03-12"), ("我出生的城市", "北京"), ("母亲的生日", "1970-01-02")]
# CLI --password-stdin 时，密码后面跟 3 行「问题编号=答案」
QUESTIONS_STDIN = "1=1998-03-12\n2=北京\n4=1970-01-02\n"

# 测试（含它启动的子进程脚本）的审计日志 / 设置一律写到临时目录，不碰真实的
# ~/Library/Application Support/文件保险箱（以前测试会往真实审计日志里写「创建保险箱」记录）
import tempfile  # noqa: E402

os.environ.setdefault("FDVAULT_DATA_DIR", tempfile.mkdtemp(prefix="fdvault-test-data-"))


# ---------------------------------------------------------------- 环境

@pytest.fixture(autouse=True)
def fast_env(monkeypatch):
    monkeypatch.setenv("FDVAULT_KDF_FAST", "1")
    monkeypatch.setenv("FDVAULT_CHUNK_SIZE", "65536")
    monkeypatch.setenv("FDVAULT_SKIP_BUSY_CHECK", "1")
    from core import crash

    crash.reset_for_tests()
    yield
    crash.reset_for_tests()


@pytest.fixture(autouse=True)
def release_protection(tmp_path):
    """上锁的保险箱带防删除保护；测试结束撤掉，pytest 才能清理临时目录。"""
    yield
    from core import protect

    protect.release_tree(tmp_path)


@pytest.fixture
def audit_dir(tmp_path: Path) -> Path:
    d = tmp_path / "audit"
    d.mkdir()
    return d


# ---------------------------------------------------------------- 数据

def write_file(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mode is not None:
        os.chmod(path, mode)


def make_tree(root: Path, *, big_size: int = 200_000) -> None:
    """一份覆盖边界情况的测试数据。"""
    root.mkdir(parents=True, exist_ok=True)
    write_file(root / "a.txt", b"hello world\n")
    write_file(root / "中文文件.txt", "中文内容测试\n".encode("utf-8"))
    write_file(root / "empty.txt", b"")
    write_file(root / "sub" / "big.bin", bytes(range(256)) * (big_size // 256) + b"tail")
    write_file(root / "sub" / "deep" / "空格 和#号.txt", b"special name\n")
    write_file(root / "sub" / "readme.only", b"x" * 100, mode=0o600)
    (root / "空目录").mkdir(exist_ok=True)
    (root / "sub" / "空子目录").mkdir(parents=True, exist_ok=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(root: Path) -> dict[str, tuple]:
    """记录明文树的 (类型, sha256, mtime_ns, mode)，跳过 .vault 和说明文件。"""
    out: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".vault")
        for d in dirnames:
            p = os.path.join(dirpath, d)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            out[rel] = ("dir", None, None, None)
        for f in sorted(filenames):
            if f == "如何解锁.txt":
                continue
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            st = os.stat(p)
            out[rel] = ("file", sha256_file(Path(p)), st.st_mtime_ns, stat.S_IMODE(st.st_mode))
    return out


# ---------------------------------------------------------------- 子进程 CLI

def run_cli(
    args: list[str],
    *,
    stdin: str = "",
    audit_dir: Path | None,
    env_extra: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(PROJECT),
            "FDVAULT_KDF_FAST": "1",
            "FDVAULT_CHUNK_SIZE": "65536",
        }
    )
    if env_extra:
        env.update(env_extra)
    cmd = [sys.executable, "-m", "cli.main", *args]
    if audit_dir is not None:
        cmd += ["--audit-dir", str(audit_dir)]
    return subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(cwd or PROJECT),
        timeout=300,
    )


def state_of(root: Path) -> str:
    from core.vault import Vault

    return Vault(root).status()["state"]
