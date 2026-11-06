"""磁盘空间检查测试（磁盘只剩 10% 的机器要用得上）。"""

from __future__ import annotations

import core.fsutil as fsmod
from core import constants as C
from core.errors import DiskSpaceError
from core.vault import Vault
from tests.conftest import PASSWORD, QUESTIONS, make_tree


def test_create_refuses_when_low_space(monkeypatch, tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    monkeypatch.setattr(fsmod, "free_bytes", lambda path: 1024)
    v = Vault(root, audit_dir=audit_dir)
    try:
        v.create(PASSWORD, QUESTIONS)
        raise AssertionError("空间不足时应该拒绝创建")
    except DiskSpaceError as exc:
        assert "磁盘空间不够" in str(exc)
    assert not (root / ".vault").exists()


def test_unlock_refuses_when_low_space(monkeypatch, tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    v = Vault(root, audit_dir=audit_dir)
    v.create(PASSWORD, QUESTIONS)
    v.close()

    monkeypatch.setattr(fsmod, "free_bytes", lambda path: 1024)
    v = Vault(root, audit_dir=audit_dir)
    try:
        v.unlock(password=PASSWORD)
        raise AssertionError("空间不足时应该拒绝解锁")
    except DiskSpaceError:
        pass
    v.close()
    assert Vault(root).status()["state"] == C.LOCKED
