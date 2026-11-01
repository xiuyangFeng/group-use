"""篡改 / 损坏检测。"""

from __future__ import annotations

import json
import os
import struct

import pytest

from core import constants as C
from core.errors import IntegrityError, NotAVaultError
from core.transform import HEADER as CHUNK_HEADER
from core.vault import Vault
from tests.conftest import PASSWORD, make_tree


def locked_vault(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    v = Vault(root, audit_dir=audit_dir)
    v.create(PASSWORD)
    v.close()
    return root


def blobs(root):
    return sorted((root / ".vault" / "blobs").iterdir())


def test_flip_ciphertext_byte_detected(tmp_path, audit_dir):
    root = locked_vault(tmp_path, audit_dir)
    blob = blobs(root)[0]
    data = bytearray(blob.read_bytes())
    data[-5] ^= 0xFF
    blob.write_bytes(data)

    v = Vault(root, audit_dir=audit_dir)
    result = v.verify(password=PASSWORD)
    v.close()
    assert not result["ok"]
    assert len(result["problems"]) == 1


def test_truncated_blob_detected(tmp_path, audit_dir):
    root = locked_vault(tmp_path, audit_dir)
    blob = blobs(root)[0]
    data = blob.read_bytes()
    blob.write_bytes(data[: len(data) // 2])

    v = Vault(root, audit_dir=audit_dir)
    result = v.verify(password=PASSWORD)
    v.close()
    assert not result["ok"]


def test_chunk_reorder_detected(tmp_path, audit_dir):
    """把同一个文件里的两个密文块调换顺序，必须被发现。"""
    root = locked_vault(tmp_path, audit_dir)
    target = None
    for blob in blobs(root):
        data = blob.read_bytes()
        magic, version, chunk_size, plain_size, count = CHUNK_HEADER.unpack(data[: CHUNK_HEADER.size])
        if count >= 3:
            target = (blob, data, chunk_size, plain_size, count)
            break
    assert target is not None
    blob, data, chunk_size, plain_size, count = target
    body = data[CHUNK_HEADER.size :]
    lens = [chunk_size + 16] * (count - 1)
    lens.append(body.__len__() - sum(lens))
    chunks = []
    off = 0
    for n in lens:
        chunks.append(body[off : off + n])
        off += n
    chunks[0], chunks[2] = chunks[2], chunks[0]
    blob.write_bytes(data[: CHUNK_HEADER.size] + b"".join(chunks))

    v = Vault(root, audit_dir=audit_dir)
    result = v.verify(password=PASSWORD)
    v.close()
    assert not result["ok"]


def test_swap_blob_between_files_detected(tmp_path, audit_dir):
    root = locked_vault(tmp_path, audit_dir)
    a, b = blobs(root)[0], blobs(root)[1]
    da, db = a.read_bytes(), b.read_bytes()
    a.write_bytes(db)
    b.write_bytes(da)

    v = Vault(root, audit_dir=audit_dir)
    result = v.verify(password=PASSWORD)
    v.close()
    assert not result["ok"]
    assert len(result["problems"]) == 2


def test_header_corrupted_rejected(tmp_path, audit_dir):
    root = locked_vault(tmp_path, audit_dir)
    (root / ".vault" / "header.json").write_bytes(b"{not json")
    with pytest.raises(NotAVaultError):
        Vault(root, audit_dir=audit_dir).status()


def test_manifest_plaintext_names_absent(tmp_path, audit_dir):
    root = locked_vault(tmp_path, audit_dir)
    raw = (root / ".vault" / "manifest.db").read_bytes()
    assert b"big.bin" not in raw
    assert "中文文件".encode() not in raw


def test_wrong_state_after_manual_crash_marker(tmp_path, audit_dir):
    """header 状态被改成进行中时，普通操作应要求先恢复。"""
    from core.errors import RecoveryNeededError
    from core.header import load_header, save_header

    root = locked_vault(tmp_path, audit_dir)
    hp = root / ".vault" / "header.json"
    h = load_header(hp)
    h.set_state(C.UNLOCKING)
    save_header(hp, h)

    v = Vault(root, audit_dir=audit_dir)
    with pytest.raises(RecoveryNeededError):
        v.unlock(password=PASSWORD)
    v.close()


# ---------------------------------------------------------------- 互斥 / 元数据

def test_second_instance_rejected(tmp_path, audit_dir):
    """另一个进程正在操作时，应拒绝而不是并发写坏数据。"""
    from core.lockfile import VaultLock
    from tests.conftest import run_cli

    root = tmp_path / "data"
    make_tree(root)
    v = Vault(root, audit_dir=audit_dir)
    v.create(PASSWORD)
    v.close()

    lock = VaultLock(root / ".vault" / "instance.lock")
    lock.acquire()
    try:
        r = run_cli(
            ["unlock", str(root), "--password-stdin", "--no-progress"],
            stdin=PASSWORD + "\n",
            audit_dir=audit_dir,
        )
    finally:
        lock.release()
    assert r.returncode == 1, r.stderr
    assert "另一个窗口" in r.stderr
    # 释放后可以正常操作
    r = run_cli(
        ["unlock", str(root), "--password-stdin", "--no-progress"],
        stdin=PASSWORD + "\n",
        audit_dir=audit_dir,
    )
    assert r.returncode == 0, r.stderr


def test_missing_manifest_blocks_unlock_and_verify(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    v = Vault(root, audit_dir=audit_dir)
    v.create(PASSWORD)
    v.close()

    for p in (root / ".vault").glob("manifest.db*"):
        p.unlink()

    v = Vault(root, audit_dir=audit_dir)
    with pytest.raises(IntegrityError):
        v.unlock(password=PASSWORD)
    v.close()
    assert Vault(root).status()["state"] == C.LOCKED

    v = Vault(root, audit_dir=audit_dir)
    with pytest.raises(IntegrityError):
        v.verify(password=PASSWORD)
    v.close()


def test_manifest_rebuilt_on_lock_if_deleted_while_unlocked(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    before = None
    from tests.conftest import snapshot

    before = snapshot(root)
    v = Vault(root, audit_dir=audit_dir)
    v.create(PASSWORD)
    v.unlock(password=PASSWORD)
    for p in (root / ".vault").glob("manifest.db*"):
        p.unlink()
    v.lock(password=PASSWORD)  # 从上锁时的明文树重建 manifest
    v.close()
    assert Vault(root).status()["state"] == C.LOCKED

    v = Vault(root, audit_dir=audit_dir)
    v.unlock(password=PASSWORD)
    v.close()
    assert snapshot(root) == before
