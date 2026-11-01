"""文件被占用时的行为：上锁前检查、删不掉时给友好错误、密码错多次后的延迟。"""

from __future__ import annotations

import errno
import os
import sys
import time
from pathlib import Path

import pytest

from core import constants as C
from core import fsutil
from core.errors import PasswordError, VaultError
from core.vault import Vault
from tests.conftest import PASSWORD, make_tree, snapshot, write_file


def _new_vault(tmp_path, name="保险箱"):
    root = tmp_path / name
    make_tree(root)
    before = snapshot(root)
    v = Vault(root, audit_dir=tmp_path / "audit")
    v.create(PASSWORD)
    v.close()
    return root, before


def test_busy_files_detects_open_file(tmp_path, monkeypatch):
    """macOS 上 lsof 能发现被 Python 自己打开的文件；Windows 分支在真机另测。"""
    monkeypatch.delenv("FDVAULT_SKIP_BUSY_CHECK", raising=False)
    f = tmp_path / "打开的文件.txt"
    f.write_text("x", "utf-8")
    with open(f, "rb"):
        busy = fsutil.busy_files([f])
    if os.name == "posix" and os.uname().sysname == "Darwin":
        assert f in busy, busy
    else:  # pragma: no cover - Windows/Linux 行为不同
        assert isinstance(busy, list)


def test_lock_aborts_when_file_busy(tmp_path, monkeypatch):
    root, before = _new_vault(tmp_path)
    v = Vault(root, audit_dir=tmp_path / "audit")
    v.unlock(PASSWORD)
    busy_file = root / "患者名单.txt"
    write_file(busy_file, b"busy")
    before = snapshot(root)

    monkeypatch.delenv("FDVAULT_SKIP_BUSY_CHECK", raising=False)
    monkeypatch.setattr("core.vault.fsutil.busy_files", lambda paths: [busy_file])
    with pytest.raises(VaultError) as ei:
        v.lock()
    assert "正在被其他程序使用" in str(ei.value)
    assert Vault(root).status()["state"] == C.UNLOCKED, "检查失败时不能改动状态"
    assert snapshot(root) == before, "检查失败时不能动文件"
    v.close()


def test_lock_unlink_permission_error_is_friendly(tmp_path, monkeypatch):
    root, before = _new_vault(tmp_path)
    v = Vault(root, audit_dir=tmp_path / "audit")
    v.unlock(PASSWORD)

    real_unlink = os.unlink
    plain_a = root / "a.txt"

    def fake_unlink(path, *args, **kwargs):
        if os.path.abspath(str(path)) == os.path.abspath(str(plain_a)):
            raise PermissionError(errno.EACCES, "被占用", str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr("core.vault.os.unlink", fake_unlink)
    with pytest.raises(VaultError) as ei:
        v.lock()
    assert "删不掉" in str(ei.value)
    assert Vault(root).status()["state"] == C.LOCKING

    # 关掉占用后可以继续把锁上完（恢复是幂等的），数据必须完好
    monkeypatch.setattr("core.vault.os.unlink", real_unlink)
    v.close()
    v2 = Vault(root, audit_dir=tmp_path / "audit")
    v2.recover(PASSWORD, rollback=False)
    v2.close()
    assert Vault(root).status()["state"] == C.LOCKED
    v3 = Vault(root, audit_dir=tmp_path / "audit")
    v3.unlock(PASSWORD)
    v3.close()
    assert snapshot(root) == before


def test_password_throttle_counts_and_resets(tmp_path):
    root, _ = _new_vault(tmp_path)
    for i in range(C.MAX_FREE_ATTEMPTS):
        v = Vault(root, audit_dir=tmp_path / "audit")
        with pytest.raises(PasswordError):
            v.unlock("wrong-password")
        hdr = v.header
        assert hdr.failed_attempts == i + 1, (i, hdr.failed_attempts)
        assert hdr.last_failed_at
        v.close()

    # 第 6 次：测试模式只等 0.02 秒，但计数继续增加
    v = Vault(root, audit_dir=tmp_path / "audit")
    started = time.monotonic()
    with pytest.raises(PasswordError):
        v.unlock("wrong-password")
    assert v.header.failed_attempts == C.MAX_FREE_ATTEMPTS + 1
    assert time.monotonic() - started < 5  # FDVAULT_KDF_FAST 下不应真等 10 秒
    v.close()

    # 密码正确后计数清零
    v = Vault(root, audit_dir=tmp_path / "audit")
    v.unlock(PASSWORD)
    assert v.header.failed_attempts == 0
    assert v.header.last_failed_at == ""
    v.close()


def test_busy_windows_helper_detects_share_conflict(tmp_path, monkeypatch):
    """Windows 分支：os.open 抛 PermissionError 且文件可写 → 视为占用；只读文件不算。"""
    import core.fsutil as fsutil

    normal = tmp_path / "normal.txt"
    busy = tmp_path / "busy.xlsx"
    readonly = tmp_path / "readonly.txt"
    for p in (normal, busy, readonly):
        p.write_text("x", encoding="utf-8")

    real_open = os.open
    real_access = os.access

    def fake_open(path, flags, *args, **kwargs):
        name = Path(path).name
        if name in ("busy.xlsx", "readonly.txt"):
            raise PermissionError(13, "共享冲突", path)
        return real_open(path, flags, *args, **kwargs)

    def fake_access(path, mode):
        return Path(path).name != "readonly.txt"

    monkeypatch.setattr(fsutil.os, "open", fake_open)
    monkeypatch.setattr(fsutil.os, "access", fake_access)
    result = fsutil._busy_windows([normal, busy, readonly])
    assert result == [busy], result


def test_busy_dispatch_and_skip(tmp_path, monkeypatch):
    import core.fsutil as fsutil

    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    monkeypatch.delenv("FDVAULT_SKIP_BUSY_CHECK", raising=False)
    monkeypatch.setattr(fsutil, "IS_WINDOWS", True)
    monkeypatch.setattr(fsutil, "_busy_windows", lambda paths: ["windows分支"])
    assert fsutil.busy_files([f]) == ["windows分支"]
    monkeypatch.setenv("FDVAULT_SKIP_BUSY_CHECK", "1")
    assert fsutil.busy_files([f]) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="lsof 仅在 macOS 上启用")
def test_busy_macos_resolves_symlink_paths(tmp_path):
    """lsof 返回 /private/var/... 而传入 /var/... 时也要能匹配上（realpath 归一化）。"""
    import subprocess
    import time

    from core import fsutil

    real = tmp_path / "real"
    real.mkdir()
    target = real / "占用 文件.txt"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", f"import time; f=open({str(target)!r}, 'rb'); time.sleep(30)"]
    )
    try:
        time.sleep(0.8)
        assert fsutil._busy_macos([link / "占用 文件.txt"]) == [link / "占用 文件.txt"]
    finally:
        holder.kill()
        holder.wait()
