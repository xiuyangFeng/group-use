"""暂停 / 取消 / 两阶段创建（GUI 进度条依赖这些能力）。"""

from __future__ import annotations

import os
import threading
import time

import pytest

from core import constants as C
from core.control import OperationControl
from core.errors import Cancelled
from core.vault import Vault
from tests.conftest import PASSWORD, QUESTIONS, make_tree, snapshot


def _make_vault(root, audit_dir):
    v = Vault(root, audit_dir=audit_dir)
    v.create(PASSWORD, QUESTIONS)
    v.close()


def test_two_phase_create(tmp_path, audit_dir):
    """GUI 创建向导：先设好密码和密保（生成 header），再执行首次上锁。"""
    root = tmp_path / "data"
    make_tree(root)
    before = snapshot(root)

    v = Vault(root, audit_dir=audit_dir)
    prepared = v.prepare_new(PASSWORD, QUESTIONS)
    assert v.status()["state"] == C.LOCKING
    assert prepared["files"] == 6 and prepared["dirs"] == 4
    assert prepared["questions"] == [q for q, _a in QUESTIONS]
    # 新保险箱没有恢复密钥，只有密保问题
    assert not v.header.has_recovery_key and v.header.recovery_fingerprint == ""

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
    v.prepare_new(PASSWORD, QUESTIONS)
    assert v.abort_create() is True
    v.close()

    assert not (root / ".vault").exists()
    assert not (root / "如何解锁.txt").exists()
    assert snapshot(root) == before
    assert Vault(root).is_vault() is False


def _assert_plain_folder(root, before):
    assert not (root / C.VAULT_DIRNAME).exists()
    assert not (root / C.MARKER_NAME).exists()
    assert Vault(root).is_vault() is False
    assert snapshot(root) == before


def test_cancel_initial_lock_at_every_point_restores_plain_folder(tmp_path, audit_dir):
    """新建保险箱、首次上锁做到一半点「取消」：不管停在哪一步，都变回原来的普通文件夹。"""
    points = 0
    for n in range(1, 200):
        root = tmp_path / f"data{n}"
        make_tree(root)
        before = snapshot(root)
        v = Vault(root, audit_dir=audit_dir)
        v.prepare_new(PASSWORD, QUESTIONS)
        control = OperationControl()
        control.cancel_after = n
        try:
            v.initial_lock(control=control)
        except Cancelled:
            points += 1
            v.cancel_create()
            v.close()
            _assert_plain_folder(root, before)
            continue
        v.close()
        break
    else:
        pytest.fail("首次上锁一直没做完")
    assert points >= 3, points


def test_cancel_create_before_any_file_touched(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    before = snapshot(root)
    v = Vault(root, audit_dir=audit_dir)
    v.prepare_new(PASSWORD, QUESTIONS)
    control = OperationControl()
    control.cancel()
    with pytest.raises(Cancelled):
        v.initial_lock(control=control)
    v.cancel_create()
    v.close()
    _assert_plain_folder(root, before)


def test_cancel_create_refuses_existing_vault(tmp_path, audit_dir):
    """已经上过锁的保险箱不能「取消新建」（那要用解除保险箱，需要密码）。"""
    from core.errors import VaultStateError

    root = tmp_path / "data"
    make_tree(root)
    _make_vault(root, audit_dir)
    v = Vault(root, audit_dir=audit_dir)
    with pytest.raises(VaultStateError):
        v.cancel_create()
    v.close()
    assert Vault(root).status()["state"] == C.LOCKED

    # 同一个对象首次上锁成功之后也不行
    root2 = tmp_path / "data2"
    make_tree(root2)
    v = Vault(root2, audit_dir=audit_dir)
    v.prepare_new(PASSWORD, QUESTIONS)
    v.initial_lock()
    with pytest.raises(VaultStateError):
        v.cancel_create()
    v.close()
    assert Vault(root2).status()["state"] == C.LOCKED


def test_cancel_initial_lock_after_first_batch_restores_deleted_files(tmp_path, audit_dir):
    """文件多时分批加密：前一批的明文已经删掉了才取消，也要全部解密回来。"""
    from core.vault import BATCH

    root = tmp_path / "data"
    n_files = BATCH + 100
    for i in range(n_files):
        p = root / f"d{i % 7}" / f"f{i:05d}.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f"file {i}\n".encode() * (i % 13 + 1))
    before = snapshot(root)

    def plain_count() -> int:
        return sum(len([f for f in fs if f.endswith(".txt")]) for d, _, fs in os.walk(root) if ".vault" not in d)

    control = OperationControl()
    seen_deleted = []

    def on_check(c):
        if not seen_deleted and plain_count() < n_files:
            seen_deleted.append(True)
            c.cancel()

    control.on_check = on_check
    v = Vault(root, audit_dir=audit_dir)
    v.prepare_new(PASSWORD, QUESTIONS)
    with pytest.raises(Cancelled):
        v.initial_lock(control=control)
    assert seen_deleted and plain_count() < n_files, "应该在部分明文已删掉之后取消"
    v.cancel_create()
    v.close()
    _assert_plain_folder(root, before)
