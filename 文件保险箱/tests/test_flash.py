"""闪电锁（不加密，只藏起来 + 禁止访问）：1 秒级上锁 / 解锁；任意一步断电、取消、失败都不丢文件。"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

from core import constants as C
from core import flash, fsutil, protect
from core.control import OperationControl
from core.errors import Cancelled, PasswordError, VaultError, VaultStateError
from core.vault import Vault
from tests.conftest import PASSWORD, QUESTIONS, QUESTIONS_STDIN, make_tree, run_cli, snapshot, state_of

IS_WINDOWS = os.name == "nt"


def _create(root, audit_dir):
    v = Vault(root, audit_dir=audit_dir)
    stats = v.create(PASSWORD, QUESTIONS, mode=C.MODE_FLASH)["stats"]
    v.close()
    return stats


def _op(root, audit_dir, name, **kw):
    v = Vault(root, audit_dir=audit_dir)
    try:
        return getattr(v, name)(**kw)
    finally:
        v.close()


def _visible(root) -> list[str]:
    return sorted(os.listdir(root))


@pytest.fixture
def flash_locked(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    expected = snapshot(root)
    _create(root, audit_dir)
    return root, audit_dir, expected


# ---------------------------------------------------------------- 正常流程

def test_roundtrip_keeps_everything(flash_locked):
    root, audit_dir, expected = flash_locked
    assert state_of(root) == C.LOCKED
    assert _visible(root) == sorted([C.VAULT_DIRNAME, C.MARKER_NAME])
    st = Vault(root).status()
    assert st["mode"] == C.MODE_FLASH and st["file_count"] == 5  # 最上面一层 5 项

    _op(root, audit_dir, "unlock", password=PASSWORD)
    assert state_of(root) == C.UNLOCKED
    assert snapshot(root) == expected  # 内容、修改时间、权限都不变
    assert not flash.map_path(Vault(root)).exists()

    # 解锁期间改一改，再锁再开
    (root / "a.txt").write_bytes(b"changed\n")
    (root / "新文件.txt").write_bytes(b"new\n")
    expected = snapshot(root)
    _op(root, audit_dir, "lock", password=PASSWORD)
    assert _visible(root) == sorted([C.VAULT_DIRNAME, C.MARKER_NAME])
    _op(root, audit_dir, "unlock", password=PASSWORD)
    assert snapshot(root) == expected


def test_header_is_version_3_and_names_are_encrypted(flash_locked):
    root, _audit, _exp = flash_locked
    data = json.loads((root / C.VAULT_DIRNAME / C.HEADER_NAME).read_text(encoding="utf-8"))
    assert data["version"] == C.FORMAT_VERSION_FLASH and data["mode"] == C.MODE_FLASH
    raw = (root / C.VAULT_DIRNAME / C.FLASH_MAP_NAME).read_text(encoding="utf-8")
    for name in ("a.txt", "sub", "中文文件", "空目录"):
        assert name not in raw, "对照表里不能有明文名字"
    marker = (root / C.MARKER_NAME).read_text(encoding="utf-8")
    assert "闪电锁" in marker


def test_wrong_password_does_not_open(flash_locked):
    root, audit_dir, _exp = flash_locked
    with pytest.raises(PasswordError):
        _op(root, audit_dir, "unlock", password="000000")
    assert state_of(root) == C.LOCKED
    assert _visible(root) == sorted([C.VAULT_DIRNAME, C.MARKER_NAME])


def test_unlock_with_security_answer_and_change_password(flash_locked):
    root, audit_dir, expected = flash_locked
    _op(root, audit_dir, "unlock", answers=[None, "北京市", None])
    assert snapshot(root) == expected
    _op(root, audit_dir, "change_password", old_password=PASSWORD, new_password="135790")
    _op(root, audit_dir, "lock", password="135790")
    _op(root, audit_dir, "unlock", password="135790")
    assert snapshot(root) == expected


def test_verify_and_dissolve(flash_locked):
    root, audit_dir, expected = flash_locked
    r = _op(root, audit_dir, "verify", password=PASSWORD)
    assert r["files"] == 5 and r["problems"] == []
    _op(root, audit_dir, "dissolve", password=PASSWORD)
    assert not (root / C.VAULT_DIRNAME).exists() and not (root / C.MARKER_NAME).exists()
    assert snapshot(root) == expected


def test_empty_folder(tmp_path, audit_dir):
    root = tmp_path / "empty"
    root.mkdir()
    assert _create(root, audit_dir)["files"] == 0
    _op(root, audit_dir, "unlock", password=PASSWORD)
    assert _visible(root) == sorted([C.VAULT_DIRNAME, C.MARKER_NAME])


def test_lock_time_does_not_depend_on_file_count(tmp_path, audit_dir, monkeypatch):
    """上万个文件也只挪最上面几项：不扫目录、不逐个处理文件。"""
    monkeypatch.setenv("FDVAULT_SKIP_BUSY_CHECK", "1")
    root = tmp_path / "many"
    for i in range(20000):
        p = root / f"d{i % 4}" / f"s{i % 50}" / f"f{i}.txt"
        if i < 200:
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    deep = root / "d0" / "s0" / "f0.txt"
    fp_before = fsutil_fp(deep)
    t = time.perf_counter()
    _create(root, audit_dir)
    created = time.perf_counter() - t
    t = time.perf_counter()
    _op(root, audit_dir, "unlock", password=PASSWORD)
    _op(root, audit_dir, "lock", password=PASSWORD)
    _op(root, audit_dir, "unlock", password=PASSWORD)
    cycle = time.perf_counter() - t
    assert created < 5 and cycle < 5, (created, cycle)
    # 深处的文件一次都没被碰过（Windows 上也没有被整棵树重写权限）
    assert fsutil_fp(deep) == fp_before


def fsutil_fp(p):
    from core import transform

    return transform.stat_many([p])[0]


# ---------------------------------------------------------------- 保护

def test_stash_contents_are_not_walked_by_protection(flash_locked):
    root, _audit, _exp = flash_locked
    dirs, files = protect._vault_entries(root)
    stash = root / C.VAULT_DIRNAME / C.STASH_DIRNAME
    assert stash in dirs
    assert not any(stash in p.parents for p in dirs + files)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS 权限")
def test_macos_stash_is_unreadable_while_locked(flash_locked):
    root, audit_dir, _exp = flash_locked
    stash = root / C.VAULT_DIRNAME / C.STASH_DIRNAME
    assert protect.is_protected(root)
    assert stash.stat().st_mode & 0o777 == 0
    with pytest.raises(PermissionError):
        os.listdir(stash)
    _op(root, audit_dir, "unlock", password=PASSWORD)
    assert stash.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows 权限")
def test_windows_stash_is_hidden_and_unlistable(flash_locked):
    root, audit_dir, expected = flash_locked
    stash = root / C.VAULT_DIRNAME / C.STASH_DIRNAME
    assert protect.is_protected(root)
    with pytest.raises(PermissionError):
        os.listdir(stash)
    with pytest.raises(OSError):
        os.rename(root / C.VAULT_DIRNAME, root / "moved")
    with pytest.raises(OSError):
        os.unlink(root / C.VAULT_DIRNAME / C.HEADER_NAME)
    import ctypes

    attrs = ctypes.windll.kernel32.GetFileAttributesW(str(root / C.VAULT_DIRNAME))
    assert attrs & 0x2, "Windows 上 .vault 应该是隐藏的"
    # 根目录上不能有可继承的规则（否则会传到每个文件，上锁就慢了）
    assert all("(I)" not in r and "(OI)" not in r and "(CI)" not in r for r in protect._win_everyone_denies(root))
    _op(root, audit_dir, "unlock", password=PASSWORD)
    assert snapshot(root) == expected
    assert not protect.is_protected(root)


# ---------------------------------------------------------------- 失败 / 取消 / 重名

def test_busy_file_blocks_lock_and_changes_nothing(tmp_path, audit_dir, monkeypatch):
    root = tmp_path / "data"
    make_tree(root)
    expected = snapshot(root)
    _create(root, audit_dir)
    _op(root, audit_dir, "unlock", password=PASSWORD)
    monkeypatch.delenv("FDVAULT_SKIP_BUSY_CHECK", raising=False)
    monkeypatch.setattr(fsutil, "busy_under", lambda r, ex=frozenset(): [str(root / "a.txt")])
    with pytest.raises(VaultError, match="正在被其他程序使用"):
        _op(root, audit_dir, "lock", password=PASSWORD)
    assert state_of(root) == C.UNLOCKED
    assert snapshot(root) == expected


@pytest.mark.parametrize("fail_at", [1, 3, 5])
def test_move_failure_rolls_everything_back(tmp_path, audit_dir, monkeypatch, fail_at):
    """Windows 上文件夹里有文件被打开时改名会失败：已经挪走的要全部挪回来。"""
    root = tmp_path / "data"
    make_tree(root)
    expected = snapshot(root)
    _create(root, audit_dir)
    _op(root, audit_dir, "unlock", password=PASSWORD)

    real = os.rename
    calls = {"n": 0}

    def flaky(src, dst):
        if C.STASH_DIRNAME in os.fspath(dst):
            calls["n"] += 1
            if calls["n"] == fail_at:
                raise PermissionError(13, "模拟：文件被占用")
        return real(src, dst)

    monkeypatch.setattr(flash.os, "rename", flaky)
    with pytest.raises(VaultError, match="正在被其他程序使用"):
        _op(root, audit_dir, "lock", password=PASSWORD)
    monkeypatch.setattr(flash.os, "rename", real)
    assert state_of(root) == C.UNLOCKED
    assert snapshot(root) == expected
    assert flash.stash_is_empty(Vault(root))


def test_move_failure_on_create_leaves_plain_folder_cancellable(tmp_path, audit_dir, monkeypatch):
    root = tmp_path / "data"
    make_tree(root)
    expected = snapshot(root)
    real = os.rename

    def flaky(src, dst):
        if C.STASH_DIRNAME in os.fspath(dst) and "sub" in os.fspath(src):
            raise PermissionError(13, "模拟：文件被占用")
        return real(src, dst)

    monkeypatch.setattr(flash.os, "rename", flaky)
    v = Vault(root, audit_dir=audit_dir)
    v.prepare_new(PASSWORD, QUESTIONS, mode=C.MODE_FLASH)
    with pytest.raises(VaultError):
        v.initial_lock()
    monkeypatch.setattr(flash.os, "rename", real)
    assert snapshot(root) == expected
    v.cancel_create()
    v.close()
    assert not (root / C.VAULT_DIRNAME).exists()
    assert snapshot(root) == expected


@pytest.mark.parametrize("n", [1, 2, 4])
def test_cancel_lock_then_rollback(tmp_path, audit_dir, n):
    root = tmp_path / "data"
    make_tree(root)
    expected = snapshot(root)
    _create(root, audit_dir)
    _op(root, audit_dir, "unlock", password=PASSWORD)
    control = OperationControl()
    control.cancel_after = n
    with pytest.raises(Cancelled):
        _op(root, audit_dir, "lock", password=PASSWORD, control=control)
    assert state_of(root) == C.LOCKING
    _op(root, audit_dir, "recover", password=PASSWORD, rollback=True)
    assert state_of(root) == C.UNLOCKED
    assert snapshot(root) == expected


@pytest.mark.parametrize("n", [1, 3])
def test_cancel_create_restores_plain_folder(tmp_path, audit_dir, n):
    root = tmp_path / "data"
    make_tree(root)
    expected = snapshot(root)
    v = Vault(root, audit_dir=audit_dir)
    v.prepare_new(PASSWORD, QUESTIONS, mode=C.MODE_FLASH)
    control = OperationControl()
    control.cancel_after = n
    with pytest.raises(Cancelled):
        v.initial_lock(control=control)
    v.cancel_create()
    v.close()
    assert not (root / C.VAULT_DIRNAME).exists() and not (root / C.MARKER_NAME).exists()
    assert snapshot(root) == expected


def test_name_taken_while_locked_is_never_overwritten(flash_locked):
    """锁着期间原位置冒出同名的东西（没保护的盘上可能发生）：两份都留着。"""
    root, audit_dir, expected = flash_locked
    protect.release(root)
    (root / "a.txt").write_bytes(b"someone else\n")
    _op(root, audit_dir, "unlock", password=PASSWORD)
    assert (root / "a.txt").read_bytes() == b"someone else\n"
    restored = root / "a（闪电锁恢复 1）.txt"
    assert restored.read_bytes() == b"hello world\n"


def test_dissolve_never_deletes_stashed_files(flash_locked):
    root, audit_dir, _exp = flash_locked
    v = Vault(root, audit_dir=audit_dir)
    v._acquire()
    try:
        with pytest.raises(VaultStateError):
            v._remove_vault_metadata()
    finally:
        v._release()
        v.close()
    assert (root / C.VAULT_DIRNAME / C.HEADER_NAME).exists()
    _op(root, audit_dir, "unlock", password=PASSWORD)


def test_abort_create_refuses_when_stash_not_empty(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    v = Vault(root, audit_dir=audit_dir)
    v.prepare_new(PASSWORD, QUESTIONS, mode=C.MODE_FLASH)
    (root / C.VAULT_DIRNAME / C.STASH_DIRNAME / "x").write_bytes(b"user data")
    assert v.abort_create() is False
    assert (root / C.VAULT_DIRNAME / C.STASH_DIRNAME / "x").exists()
    v.close()


# ---------------------------------------------------------------- 断电（子进程里直接退出）

def _cli(args, audit_dir, crash_at=None):
    env = {"FDVAULT_CRASH_AT": crash_at, "FDVAULT_SKIP_BUSY_CHECK": "1"} if crash_at else {"FDVAULT_SKIP_BUSY_CHECK": "1"}
    stdin = PASSWORD + "\n" + (QUESTIONS_STDIN if args[0] == "create" else "")
    return run_cli(args + ["--password-stdin", "--no-progress"], stdin=stdin, audit_dir=audit_dir, env_extra=env)


LOCK_POINTS = ["flash:before_map=1", "flash:after_map=1", "flash:before_move=3", "flash:after_all_moved=1"]
UNLOCK_POINTS = ["flash:before_move_back=1", "flash:before_move_back=3", "flash:before_map_unlink=1"]


@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.parametrize("point", LOCK_POINTS)
def test_crash_during_lock(tmp_path, audit_dir, point, rollback):
    root = tmp_path / "data"
    make_tree(root)
    expected = snapshot(root)
    assert _cli(["create", str(root), "--yes", "--flash"], audit_dir).returncode == 0
    assert _cli(["unlock", str(root)], audit_dir).returncode == 0
    r = _cli(["lock", str(root)], audit_dir, crash_at=point)
    assert r.returncode == 70, r.stderr
    assert state_of(root) == C.LOCKING
    args = ["recover", str(root), "--yes"] + (["--rollback"] if rollback else [])
    r = _cli(args, audit_dir)
    assert r.returncode == 0, r.stderr
    assert state_of(root) == (C.UNLOCKED if rollback else C.LOCKED)
    if not rollback:
        assert _visible(root) == sorted([C.VAULT_DIRNAME, C.MARKER_NAME])
        assert _cli(["unlock", str(root)], audit_dir).returncode == 0
    assert snapshot(root) == expected


@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.parametrize("point", UNLOCK_POINTS)
def test_crash_during_unlock(tmp_path, audit_dir, point, rollback):
    root = tmp_path / "data"
    make_tree(root)
    expected = snapshot(root)
    assert _cli(["create", str(root), "--yes", "--flash"], audit_dir).returncode == 0
    r = _cli(["unlock", str(root)], audit_dir, crash_at=point)
    assert r.returncode == 70, r.stderr
    assert state_of(root) == C.UNLOCKING
    args = ["recover", str(root), "--yes"] + (["--rollback"] if rollback else [])
    r = _cli(args, audit_dir)
    assert r.returncode == 0, r.stderr
    assert state_of(root) == (C.LOCKED if rollback else C.UNLOCKED)
    if rollback:
        assert _visible(root) == sorted([C.VAULT_DIRNAME, C.MARKER_NAME])
        assert _cli(["unlock", str(root)], audit_dir).returncode == 0
    assert snapshot(root) == expected


def test_crash_during_create(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    expected = snapshot(root)
    r = _cli(["create", str(root), "--yes", "--flash"], audit_dir, crash_at="flash:before_move=2")
    assert r.returncode == 70, r.stderr
    assert _cli(["recover", str(root), "--yes"], audit_dir).returncode == 0
    assert state_of(root) == C.LOCKED
    assert _cli(["unlock", str(root)], audit_dir).returncode == 0
    assert snapshot(root) == expected


def test_cli_status_shows_flash(flash_locked):
    root, audit_dir, _exp = flash_locked
    r = run_cli(["status", str(root)], audit_dir=audit_dir)
    assert r.returncode == 0 and "闪电锁" in r.stdout, r.stdout
