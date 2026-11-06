"""解除保险箱：文件夹变回普通文件夹，以后不再加密。"""

from __future__ import annotations

import json
import time

import pytest

from core import inuse, protect
from core.errors import AnswerError, InUseError, PasswordError, RecoveryNeededError
from core.vault import Vault
from tests.conftest import PASSWORD, QUESTIONS, make_tree, run_cli, snapshot


def _new(tmp_path, audit_dir):
    root = tmp_path / "v"
    make_tree(root)
    before = snapshot(root)
    Vault(root, audit_dir=audit_dir).create(PASSWORD, QUESTIONS)
    return root, before


def _assert_plain(root, before):
    assert not (root / ".vault").exists()
    assert not (root / "如何解锁.txt").exists()
    assert not protect.is_protected(root)
    assert not Vault(root).is_vault()
    assert snapshot(root) == before


def test_dissolve_locked_vault_with_password(tmp_path, audit_dir):
    root, before = _new(tmp_path, audit_dir)
    stats = Vault(root, audit_dir=audit_dir).dissolve(PASSWORD)
    assert stats["files"] == 6
    _assert_plain(root, before)
    events = [json.loads(line)["event"] for line in (audit_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1] == "DISSOLVE"


def test_dissolve_unlocked_vault(tmp_path, audit_dir):
    root, before = _new(tmp_path, audit_dir)
    Vault(root, audit_dir=audit_dir).unlock(PASSWORD)
    Vault(root, audit_dir=audit_dir).dissolve(PASSWORD)
    _assert_plain(root, before)


def test_dissolve_session_vault_needs_no_password_again(tmp_path, audit_dir):
    root, before = _new(tmp_path, audit_dir)
    v = Vault(root, audit_dir=audit_dir)
    v.unlock(PASSWORD)
    v.dissolve()  # 解锁时的密钥还在内存里
    _assert_plain(root, before)


def test_dissolve_with_one_answer(tmp_path, audit_dir):
    root, before = _new(tmp_path, audit_dir)
    Vault(root, audit_dir=audit_dir).dissolve(answers=[None, "北京市", None])
    _assert_plain(root, before)


def test_dissolve_wrong_password_keeps_vault(tmp_path, audit_dir):
    root, _ = _new(tmp_path, audit_dir)
    with pytest.raises(PasswordError):
        Vault(root, audit_dir=audit_dir).dissolve("000000")
    assert Vault(root).status()["state"] == "LOCKED"
    Vault(root, audit_dir=audit_dir).unlock(PASSWORD)
    with pytest.raises(PasswordError):
        Vault(root, audit_dir=audit_dir).dissolve("000000")
    with pytest.raises(AnswerError):
        Vault(root, audit_dir=audit_dir).dissolve(answers=["2000-01-01", "", ""])
    assert Vault(root).status()["state"] == "UNLOCKED"


def test_dissolve_refused_while_in_use(tmp_path, audit_dir):
    root, _ = _new(tmp_path, audit_dir)
    Vault(root, audit_dir=audit_dir).unlock(PASSWORD)
    marker = root / ".vault" / "in-use-test.json"
    marker.write_text(json.dumps({"tool": "CT 脱敏工具", "active": True, "heartbeat": time.time()}), encoding="utf-8")
    assert inuse.active_users(root)
    with pytest.raises(InUseError):
        Vault(root, audit_dir=audit_dir).dissolve(PASSWORD)
    assert Vault(root).is_vault()


def test_dissolve_refused_when_interrupted(tmp_path, audit_dir):
    root, _ = _new(tmp_path, audit_dir)
    v = Vault(root, audit_dir=audit_dir)
    protect.release(root)
    h = v.header
    h.set_state("UNLOCKING")
    v._save_header()
    with pytest.raises(RecoveryNeededError):
        Vault(root, audit_dir=audit_dir).dissolve(PASSWORD)


def test_folder_can_become_vault_again(tmp_path, audit_dir):
    root, before = _new(tmp_path, audit_dir)
    Vault(root, audit_dir=audit_dir).dissolve(PASSWORD)
    Vault(root, audit_dir=audit_dir).create(PASSWORD, QUESTIONS)
    Vault(root, audit_dir=audit_dir).unlock(PASSWORD)
    assert snapshot(root) == before


def test_cli_dissolve(tmp_path, audit_dir):
    root, before = _new(tmp_path, audit_dir)
    r = run_cli(["dissolve", str(root), "--password-stdin", "--yes"], stdin=PASSWORD + "\n", audit_dir=audit_dir)
    assert r.returncode == 0, r.stderr
    assert "不再加密" in r.stdout
    _assert_plain(root, before)


def test_cli_dissolve_forgot(tmp_path, audit_dir):
    root, before = _new(tmp_path, audit_dir)
    r = run_cli(
        ["dissolve", str(root), "--password-stdin", "--forgot", "--yes"],
        # 只答第 3 题；用纯 ASCII 答案：Windows 上 --password-stdin 按系统代码页读，不是 UTF-8
        stdin="\n\n1970-01-02\n",
        audit_dir=audit_dir,
    )
    assert r.returncode == 0, r.stderr
    _assert_plain(root, before)


def test_cli_dissolve_wrong_password(tmp_path, audit_dir):
    root, _ = _new(tmp_path, audit_dir)
    r = run_cli(["dissolve", str(root), "--password-stdin", "--yes"], stdin="000000\n", audit_dir=audit_dir)
    assert r.returncode != 0
    assert Vault(root).is_vault()

