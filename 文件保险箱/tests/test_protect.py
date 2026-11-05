"""上锁后防移动 / 防删除（core/protect.py + Vault 的 @locked 集成）。"""

from __future__ import annotations

import os
import shutil
import sys

import pytest

from core import constants as C
from core import protect
from core.errors import PasswordError
from core.vault import Vault
from tests.conftest import PASSWORD, make_tree, snapshot

pytestmark = pytest.mark.skipif(not (protect.IS_MAC or protect.IS_WINDOWS), reason="只支持 macOS / Windows")

NEW_PASSWORD = "Another-Pass-5678"


@pytest.fixture
def locked_vault(tmp_path, audit_dir):
    root = tmp_path / "数据"
    make_tree(root)
    before = snapshot(root)
    v = Vault(root, audit_dir=audit_dir)
    info = v.create(PASSWORD)
    v.close()
    return root, before, info


def test_locked_vault_cannot_be_moved_or_deleted(locked_vault, tmp_path):
    root, _, _ = locked_vault
    st = Vault(root).status()
    assert st["state"] == C.LOCKED
    assert st["protected"] is True
    elsewhere = tmp_path / "别处"
    elsewhere.mkdir()
    assert protect.probe(root, elsewhere) == []
    # 一个都没少
    assert (root / C.MARKER_NAME).exists()
    assert (root / C.VAULT_DIRNAME / C.HEADER_NAME).exists()
    assert list((root / C.VAULT_DIRNAME / C.BLOBS_DIRNAME).iterdir())
    assert not list(elsewhere.iterdir())


def test_blob_cannot_be_deleted(locked_vault):
    root, _, _ = locked_vault
    blob = next((root / C.VAULT_DIRNAME / C.BLOBS_DIRNAME).iterdir())
    with pytest.raises(OSError):
        os.unlink(blob)
    with pytest.raises(OSError):
        shutil.rmtree(root)
    assert blob.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS 的锁定标记同时防改写")
def test_mac_files_cannot_be_overwritten(locked_vault):
    root, _, _ = locked_vault
    header = root / C.VAULT_DIRNAME / C.HEADER_NAME
    before = header.read_bytes()
    with pytest.raises(OSError):
        header.write_bytes(b"{}")
    with pytest.raises(OSError):
        (root / "新文件.txt").write_text("x")
    assert header.read_bytes() == before


def test_unlock_releases_and_lock_reapplies(locked_vault, audit_dir, tmp_path):
    root, before, _ = locked_vault
    v = Vault(root, audit_dir=audit_dir)
    v.unlock(PASSWORD)
    assert snapshot(root) == before
    assert Vault(root).status()["protected"] is False
    # 解锁后是普通文件夹：能改、能移动
    (root / "a.txt").write_bytes(b"changed\n")
    moved = tmp_path / "挪过的数据"
    os.rename(root, moved)
    os.rename(moved, root)
    v.close()

    # 换一个对象上锁（模拟程序重启后用密码上锁）
    v2 = Vault(root, audit_dir=audit_dir)
    v2.lock(PASSWORD)
    v2.close()
    assert Vault(root).status()["protected"] is True
    assert protect.probe(root, tmp_path) == []

    v3 = Vault(root, audit_dir=audit_dir)
    v3.unlock(PASSWORD)
    assert (root / "a.txt").read_bytes() == b"changed\n"
    v3.lock()
    v3.close()


def test_wrong_password_keeps_protection(locked_vault, audit_dir, tmp_path):
    root, _, _ = locked_vault
    v = Vault(root, audit_dir=audit_dir)
    with pytest.raises(PasswordError):
        v.unlock("Wrong-Password-1")
    v.close()
    # 输错次数写进了 header（中途撤掉过保护），结束后保护又回来了
    assert Vault(root).header.failed_attempts == 1
    assert Vault(root).status()["protected"] is True
    assert protect.probe(root, tmp_path) == []


def test_password_ops_and_verify_work_while_locked(locked_vault, audit_dir, tmp_path):
    root, before, info = locked_vault
    v = Vault(root, audit_dir=audit_dir)
    assert v.verify(PASSWORD)["ok"]
    v.close()
    Vault(root, audit_dir=audit_dir).change_password(PASSWORD, NEW_PASSWORD)
    rec = Vault(root, audit_dir=audit_dir).regenerate_recovery_key(NEW_PASSWORD)
    assert rec["recovery_key"]
    assert Vault(root).status()["protected"] is True
    assert protect.probe(root, tmp_path) == []
    v = Vault(root, audit_dir=audit_dir)
    v.unlock(NEW_PASSWORD)
    assert snapshot(root) == before
    v.lock()
    v.close()


def test_old_unprotected_vault_gets_protected(locked_vault, tmp_path):
    root, _, _ = locked_vault
    protect.release(root)  # 模拟旧版本上锁、没有保护的保险箱
    assert Vault(root).status()["protected"] is False
    assert Vault(root).ensure_protected() is True
    assert protect.probe(root, tmp_path) == []


def test_switch_off(tmp_path, audit_dir, monkeypatch):
    monkeypatch.setenv("FDVAULT_NO_PROTECT", "1")
    root = tmp_path / "临时"
    make_tree(root)
    v = Vault(root, audit_dir=audit_dir)
    v.create(PASSWORD)
    v.close()
    assert Vault(root).status()["protected"] is False
    assert Vault(root).ensure_protected() is False
    shutil.rmtree(root)  # 关掉开关后就是普通文件夹，能直接删


def test_release_tree_allows_cleanup(locked_vault, tmp_path):
    root, _, _ = locked_vault
    protect.release_tree(tmp_path)
    shutil.rmtree(root)
    assert not root.exists()
