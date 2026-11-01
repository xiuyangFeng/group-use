"""断电/崩溃注入测试：每一步强制杀进程，再恢复，数据必须一字节不差。

注入通过环境变量 FDVAULT_CRASH_AT 完成（命中点直接 os._exit(70)），
子进程崩溃后用 CLI 的 recover 继续或回滚，然后比对整棵明文树。
"""

from __future__ import annotations

import pytest

from tests.conftest import PASSWORD, make_tree, run_cli, snapshot, state_of

UNLOCK_POINTS = [
    "unlock:after_blob_read_verify",
    "unlock:before_rename",
    "unlock:after_rename",
    "unlock:before_manifest_commit",
    "unlock:after_manifest_commit",
    "unlock:before_unlink_blob",
    "unlock:after_unlink_blob",
]

LOCK_POINTS = [
    "lock:before_blob_rename",
    "lock:after_blob_rename",
    "lock:before_manifest_commit",
    "lock:after_manifest_commit",
    "lock:before_unlink_plaintext",
    "lock:after_unlink_plaintext",
]


# ---------------------------------------------------------------- 工具

def _create(root, audit_dir):
    r = run_cli(
        ["create", str(root), "--password-stdin", "--yes", "--no-progress"],
        stdin=PASSWORD + "\n",
        audit_dir=audit_dir,
    )
    assert r.returncode == 0, r.stderr
    return r


def _unlock(root, audit_dir, crash_at: str | None = None):
    env = {"FDVAULT_CRASH_AT": crash_at} if crash_at else None
    return run_cli(
        ["unlock", str(root), "--password-stdin", "--no-progress"],
        stdin=PASSWORD + "\n",
        audit_dir=audit_dir,
        env_extra=env,
    )


def _lock(root, audit_dir, crash_at: str | None = None):
    env = {"FDVAULT_CRASH_AT": crash_at} if crash_at else None
    return run_cli(
        ["lock", str(root), "--password-stdin", "--no-progress"],
        stdin=PASSWORD + "\n",
        audit_dir=audit_dir,
        env_extra=env,
    )


def _recover(root, audit_dir, rollback: bool = False, crash_at: str | None = None):
    args = ["recover", str(root), "--password-stdin", "--no-progress", "--yes"]
    if rollback:
        args.append("--rollback")
    env = {"FDVAULT_CRASH_AT": crash_at} if crash_at else None
    return run_cli(args, stdin=PASSWORD + "\n", audit_dir=audit_dir, env_extra=env)


@pytest.fixture
def locked_case(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    expected = snapshot(root)
    _create(root, audit_dir)
    assert state_of(root) == "LOCKED"
    return root, audit_dir, expected


@pytest.fixture
def unlocked_case(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    expected = snapshot(root)
    _create(root, audit_dir)
    r = _unlock(root, audit_dir)
    assert r.returncode == 0, r.stderr
    assert state_of(root) == "UNLOCKED"
    return root, audit_dir, expected


# ---------------------------------------------------------------- 解锁中途崩溃

@pytest.mark.parametrize("point", UNLOCK_POINTS)
def test_unlock_crash_then_continue(locked_case, point):
    root, audit_dir, expected = locked_case
    r = _unlock(root, audit_dir, crash_at=f"{point}=1")
    assert r.returncode == 70, r.stderr
    assert state_of(root) == "UNLOCKING"

    r = _recover(root, audit_dir)
    assert r.returncode == 0, r.stderr
    assert state_of(root) == "UNLOCKED"
    assert snapshot(root) == expected

    # 恢复之后再完整走一轮，确认没有留下隐患
    assert _lock(root, audit_dir).returncode == 0
    assert _unlock(root, audit_dir).returncode == 0
    assert snapshot(root) == expected


@pytest.mark.parametrize("count", [2, 4])
def test_unlock_crash_on_later_file_then_continue(locked_case, count):
    root, audit_dir, expected = locked_case
    r = _unlock(root, audit_dir, crash_at=f"unlock:after_rename={count}")
    assert r.returncode == 70, r.stderr
    assert _recover(root, audit_dir).returncode == 0
    assert snapshot(root) == expected


def test_unlock_crash_then_rollback(locked_case):
    root, audit_dir, expected = locked_case
    r = _unlock(root, audit_dir, crash_at="unlock:after_manifest_commit=1")
    assert r.returncode == 70, r.stderr

    r = _recover(root, audit_dir, rollback=True)
    assert r.returncode == 0, r.stderr
    assert state_of(root) == "LOCKED"
    # 回滚后数据仍完整可解锁
    assert _unlock(root, audit_dir).returncode == 0
    assert snapshot(root) == expected


# ---------------------------------------------------------------- 上锁中途崩溃

@pytest.mark.parametrize("point", LOCK_POINTS)
def test_lock_crash_then_continue(unlocked_case, point):
    root, audit_dir, expected = unlocked_case
    r = _lock(root, audit_dir, crash_at=f"{point}=1")
    assert r.returncode == 70, r.stderr
    assert state_of(root) == "LOCKING"

    r = _recover(root, audit_dir)
    assert r.returncode == 0, r.stderr
    assert state_of(root) == "LOCKED"

    assert _unlock(root, audit_dir).returncode == 0
    assert snapshot(root) == expected


@pytest.mark.parametrize("count", [2, 4])
def test_lock_crash_on_later_file_then_continue(unlocked_case, count):
    root, audit_dir, expected = unlocked_case
    r = _lock(root, audit_dir, crash_at=f"lock:before_unlink_plaintext={count}")
    assert r.returncode == 70, r.stderr
    assert _recover(root, audit_dir).returncode == 0
    assert _unlock(root, audit_dir).returncode == 0
    assert snapshot(root) == expected


def test_lock_crash_then_rollback(unlocked_case):
    root, audit_dir, expected = unlocked_case
    r = _lock(root, audit_dir, crash_at="lock:after_blob_rename=2")
    assert r.returncode == 70, r.stderr

    r = _recover(root, audit_dir, rollback=True)
    assert r.returncode == 0, r.stderr
    assert state_of(root) == "UNLOCKED"
    assert snapshot(root) == expected


# ---------------------------------------------------------------- 恢复过程中再崩溃

def test_recover_crash_then_recover_again(unlocked_case):
    root, audit_dir, expected = unlocked_case
    assert _lock(root, audit_dir, crash_at="lock:before_unlink_plaintext=2").returncode == 70

    # 恢复过程中再来一次"断电"
    r = _recover(root, audit_dir, crash_at="lock:before_manifest_commit=1")
    assert r.returncode == 70, r.stderr
    assert state_of(root) == "LOCKING"

    r = _recover(root, audit_dir)
    assert r.returncode == 0, r.stderr
    assert state_of(root) == "LOCKED"
    assert _unlock(root, audit_dir).returncode == 0
    assert snapshot(root) == expected


def test_recover_with_wrong_password_does_not_touch_data(locked_case):
    root, audit_dir, expected = locked_case
    assert _unlock(root, audit_dir, crash_at="unlock:after_rename=1").returncode == 70
    r = run_cli(
        ["recover", str(root), "--password-stdin", "--no-progress", "--yes"],
        stdin="totally-wrong-pw-9999\n",
        audit_dir=audit_dir,
    )
    assert r.returncode == 3
    assert state_of(root) == "UNLOCKING"
    # 正确密码仍可恢复
    assert _recover(root, audit_dir).returncode == 0
    assert snapshot(root) == expected
