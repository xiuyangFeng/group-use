"""暂停 / 取消 / 两阶段创建（GUI 进度条依赖这些能力）。"""

from __future__ import annotations

import threading
import time

import pytest

from core import constants as C
from core.control import OperationControl
from core.errors import Cancelled
from core.vault import Vault
from tests.conftest import PASSWORD, make_tree, snapshot


def _make_vault(root, audit_dir):
    v = Vault(root, audit_dir=audit_dir)
    v.create(PASSWORD)
    v.close()


def test_two_phase_create(tmp_path, audit_dir):
    """GUI 创建向导：先拿恢复密钥给用户抄，再执行首次上锁。"""
    root = tmp_path / "data"
    make_tree(root)
    before = snapshot(root)

    v = Vault(root, audit_dir=audit_dir)
    prepared = v.prepare_new(PASSWORD)
    assert v.status()["state"] == C.LOCKING
    assert prepared["files"] == 6 and prepared["dirs"] == 4
    assert len(prepared["recovery_key"].replace("-", "")) == 32
    # 恢复密钥对应的指纹与 header 一致
    assert prepared["recovery_fingerprint"] == v.header.recovery_fingerprint

    stats = v.initial_lock()
    assert stats["files"] == 6
    v.close()
    assert Vault(root).status()["state"] == C.LOCKED

    v = Vault(root, audit_dir=audit_dir)
    v.unlock(password=PASSWORD)
    v.close()
    assert snapshot(root) == before


def test_cancel_unlock_then_rollback(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    before = snapshot(root)
    _make_vault(root, audit_dir)

    control = OperationControl()
    control.cancel_after = 3
    v = Vault(root, audit_dir=audit_dir)
    with pytest.raises(Cancelled):
        v.unlock(password=PASSWORD, control=control)
    v.close()
    assert Vault(root).status()["state"] == C.UNLOCKING

    v = Vault(root, audit_dir=audit_dir)
    v.recover(password=PASSWORD, rollback=True)
    v.close()
    assert Vault(root).status()["state"] == C.LOCKED

    v = Vault(root, audit_dir=audit_dir)
    v.unlock(password=PASSWORD)
    v.close()
    assert snapshot(root) == before


def test_cancel_lock_then_rollback(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    before = snapshot(root)
    _make_vault(root, audit_dir)
    v = Vault(root, audit_dir=audit_dir)
    v.unlock(password=PASSWORD)
    v.close()

    control = OperationControl()
    control.cancel_after = 4
    v = Vault(root, audit_dir=audit_dir)
    with pytest.raises(Cancelled):
        v.lock(password=PASSWORD, control=control)
    v.close()
    assert Vault(root).status()["state"] == C.LOCKING

    v = Vault(root, audit_dir=audit_dir)
    v.recover(password=PASSWORD, rollback=True)
    v.close()
    assert Vault(root).status()["state"] == C.UNLOCKED
    assert snapshot(root) == before


def test_pause_blocks_until_resume(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    _make_vault(root, audit_dir)
    v = Vault(root, audit_dir=audit_dir)
    v.unlock(password=PASSWORD)
    v.close()

    control = OperationControl()
    control.pause()
    result: dict = {}

    def run():
        v = Vault(root, audit_dir=audit_dir)
        try:
            v.lock(password=PASSWORD, control=control)
            result["ok"] = True
        except Exception as exc:  # noqa: BLE001
            result["err"] = exc
        finally:
            v.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.6)
    assert t.is_alive(), "应停在暂停点"
    assert Vault(root).status()["state"] == C.LOCKING
    control.resume()
    t.join(timeout=120)
    assert result.get("ok"), result
    assert Vault(root).status()["state"] == C.LOCKED


def test_cancel_verify(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    _make_vault(root, audit_dir)
    control = OperationControl()
    control.cancel_after = 2
    v = Vault(root, audit_dir=audit_dir)
    with pytest.raises(Cancelled):
        v.verify(password=PASSWORD, control=control)
    v.close()
    # 校验取消不影响数据
    assert Vault(root).status()["state"] == C.LOCKED


def test_abort_create_before_initial_lock(tmp_path, audit_dir):
    """创建向导在首次上锁前取消：文件夹应恢复原样。"""
    root = tmp_path / "data"
    make_tree(root)
    before = snapshot(root)

    v = Vault(root, audit_dir=audit_dir)
    v.prepare_new(PASSWORD)
    assert v.abort_create() is True
    v.close()

    assert not (root / ".vault").exists()
    assert not (root / "如何解锁.txt").exists()
    assert snapshot(root) == before
    assert Vault(root).is_vault() is False
