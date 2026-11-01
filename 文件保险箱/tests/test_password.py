"""密码 / 恢复密钥 / 改密相关测试。"""

from __future__ import annotations

import pytest

from core import constants as C, crypto
from core.errors import PasswordError, RecoveryKeyError
from core.vault import Vault
from tests.conftest import PASSWORD, make_tree

NEW_PW = "new-password-5678"


def setup(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    v = Vault(root, audit_dir=audit_dir)
    res = v.create(PASSWORD)
    v.close()
    return root, res


def test_change_password(tmp_path, audit_dir):
    root, _ = setup(tmp_path, audit_dir)
    Vault(root, audit_dir=audit_dir).change_password(PASSWORD, NEW_PW)

    v = Vault(root, audit_dir=audit_dir)
    with pytest.raises(PasswordError):
        v.unlock(password=PASSWORD)
    v = Vault(root, audit_dir=audit_dir)
    stats = v.unlock(password=NEW_PW)
    v.lock(password=NEW_PW)
    v.close()
    assert stats["files"] == 6


def test_change_password_wrong_old(tmp_path, audit_dir):
    root, _ = setup(tmp_path, audit_dir)
    with pytest.raises(PasswordError):
        Vault(root, audit_dir=audit_dir).change_password("wrong-old-pw-123", NEW_PW)


def test_reset_with_recovery_key(tmp_path, audit_dir):
    root, res = setup(tmp_path, audit_dir)
    key = crypto.recovery_key_parse(res["recovery_key"])
    Vault(root, audit_dir=audit_dir).reset_with_recovery_key(key, NEW_PW)

    v = Vault(root, audit_dir=audit_dir)
    with pytest.raises(PasswordError):
        v.unlock(password=PASSWORD)
    v = Vault(root, audit_dir=audit_dir)
    v.unlock(password=NEW_PW)
    v.lock(password=NEW_PW)
    v.close()
    assert Vault(root).status()["state"] == C.LOCKED


def test_wrong_recovery_key(tmp_path, audit_dir):
    root, _ = setup(tmp_path, audit_dir)
    bogus = crypto.recovery_key_parse("A" * 32)
    with pytest.raises(RecoveryKeyError):
        Vault(root, audit_dir=audit_dir).reset_with_recovery_key(bogus, NEW_PW)


def test_unlock_with_recovery_key(tmp_path, audit_dir):
    root, res = setup(tmp_path, audit_dir)
    key = crypto.recovery_key_parse(res["recovery_key"])
    v = Vault(root, audit_dir=audit_dir)
    v.unlock(recovery_key=key)
    assert Vault(root).status()["state"] == C.UNLOCKED
    v.lock(password=PASSWORD)
    v.close()


def test_regenerate_recovery_key_invalidates_old(tmp_path, audit_dir):
    root, res = setup(tmp_path, audit_dir)
    new = Vault(root, audit_dir=audit_dir).regenerate_recovery_key(PASSWORD)
    old_key = crypto.recovery_key_parse(res["recovery_key"])
    new_key = crypto.recovery_key_parse(new["recovery_key"])
    assert old_key != new_key
    with pytest.raises(RecoveryKeyError):
        Vault(root, audit_dir=audit_dir).reset_with_recovery_key(old_key, NEW_PW)
    Vault(root, audit_dir=audit_dir).reset_with_recovery_key(new_key, NEW_PW)
    assert Vault(root).status()["recovery_fingerprint"] == new["recovery_fingerprint"]


def test_password_not_stored_in_header(tmp_path, audit_dir):
    root, res = setup(tmp_path, audit_dir)
    raw = (root / ".vault" / "header.json").read_bytes()
    assert PASSWORD.encode() not in raw
    assert res["recovery_key"].replace("-", "").encode() not in raw


def test_change_password_blocked_when_unlock_incomplete(tmp_path, audit_dir):
    """手工把状态改成 UNLOCKING 后，改密应被拒绝，避免改坏正在进行的操作。"""
    root, _ = setup(tmp_path, audit_dir)
    from core.header import load_header, save_header
    from core.errors import RecoveryNeededError

    h = load_header(root / ".vault" / "header.json")
    h.set_state(C.UNLOCKING)
    save_header(root / ".vault" / "header.json", h)
    with pytest.raises(RecoveryNeededError):
        Vault(root, audit_dir=audit_dir).change_password(PASSWORD, NEW_PW)
