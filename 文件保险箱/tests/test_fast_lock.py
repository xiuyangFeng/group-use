"""解锁保留密文 + 增量上锁：没改过的文件直接用保留的密文，改过的一定重新加密（绝不能丢用户的修改）。"""

from __future__ import annotations

import os
import sqlite3
import time

import pytest

from core import constants as C
from core.vault import Vault
from tests.conftest import PASSWORD, QUESTIONS, make_tree, snapshot


def _blob_dir(root):
    return root / C.VAULT_DIRNAME / "blobs"


def _blob_ids(root):
    """每个密文文件的 (inode, 修改时间)：重新加密会换掉文件，这两个值就会变。"""
    out = {}
    for p in _blob_dir(root).iterdir():
        st = p.stat()
        out[p.name] = (st.st_ino, st.st_mtime_ns)
    return out


def _kept_rows(root) -> int:
    # 上锁后 .vault 被系统锁定（不能建 -wal/-shm），只读 + immutable 打开
    db = (root / C.VAULT_DIRNAME / C.MANIFEST_NAME).resolve().as_uri()
    con = sqlite3.connect(f"{db}?mode=ro&immutable=1", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM kept").fetchone()[0]
    finally:
        con.close()


@pytest.fixture
def unlocked(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    v = Vault(root, audit_dir=audit_dir)
    v.create(PASSWORD, QUESTIONS)
    v.close()
    v = Vault(root, audit_dir=audit_dir)
    v.unlock(password=PASSWORD)
    v.close()
    return root, audit_dir


def _lock(root, audit_dir):
    v = Vault(root, audit_dir=audit_dir)
    v.lock(password=PASSWORD)
    v.close()


def _unlock(root, audit_dir):
    v = Vault(root, audit_dir=audit_dir)
    v.unlock(password=PASSWORD)
    v.close()


def test_unchanged_files_reuse_kept_ciphertext(unlocked):
    root, audit_dir = unlocked
    before = snapshot(root)
    blobs = _blob_ids(root)
    assert len(blobs) == 6 and _kept_rows(root) == 6
    _lock(root, audit_dir)
    assert _blob_ids(root) == blobs, "没改过的文件不应该重新加密"
    assert _kept_rows(root) == 0, "上锁完成后指纹记录应清空"
    assert [p.name for p in root.iterdir() if p.name not in (C.VAULT_DIRNAME, C.MARKER_NAME)] == []
    _unlock(root, audit_dir)
    assert snapshot(root) == before


def test_lock_start_does_not_touch_kept_fingerprints(unlocked):
    """上锁一开始会撤保护（Windows 是 icacls）：不能改动保留的密文的指纹，否则全部白白重新加密。"""
    import struct

    from core import protect, transform

    root, _ = unlocked
    db = (root / C.VAULT_DIRNAME / C.MANIFEST_NAME).resolve().as_uri()
    con = sqlite3.connect(f"{db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT uuid, blob_fp FROM kept").fetchall()
    finally:
        con.close()
    assert len(rows) == 6
    protect.release(root)
    paths = [_blob_dir(root) / (bytes(u).hex() + ".blob") for u, _ in rows]
    names = ("size", "dev", "ino", "mtime", "ctime")
    diffs = []
    for p, (_, old), new in zip(paths, rows, transform.stat_many(paths)):
        a, b = struct.unpack("<5Q", bytes(old)), struct.unpack("<5Q", new)
        diffs += [f"{p.name}.{n}: {x} -> {y}" for n, x, y in zip(names, a, b) if x != y]
    assert not diffs, "指纹变了：\n" + "\n".join(diffs)


def test_edit_is_never_lost_even_with_same_size_and_restored_mtime(unlocked):
    """最危险的情况：内容改了、大小没变、修改时间还被改回原样。靠状态改变时间（ctime）识别。"""
    root, audit_dir = unlocked
    p = root / "a.txt"
    st = p.stat()
    old = p.read_bytes()
    new = old.upper()
    assert len(new) == len(old) and new != old
    time.sleep(0.01)
    p.write_bytes(new)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))  # 把修改时间改回去
    assert p.stat().st_mtime_ns == st.st_mtime_ns and p.stat().st_size == st.st_size
    expected = snapshot(root)
    _lock(root, audit_dir)
    _unlock(root, audit_dir)
    assert p.read_bytes() == new
    assert snapshot(root) == expected


def test_edited_and_new_files_are_encrypted(unlocked):
    root, audit_dir = unlocked
    (root / "sub" / "big.bin").write_bytes(b"\x01" * 300_000)
    (root / "新文件.txt").write_bytes("解锁期间新建\n".encode())
    expected = snapshot(root)
    blobs = _blob_ids(root)
    _lock(root, audit_dir)
    after = _blob_ids(root)
    assert len(after) == 7
    changed = {k for k in blobs if after.get(k) != blobs[k]}
    assert len(changed) == 1, "只有改过的那个文件的密文被换掉"
    _unlock(root, audit_dir)
    assert snapshot(root) == expected


def test_deleted_and_renamed_files_leave_no_orphan_ciphertext(unlocked):
    root, audit_dir = unlocked
    os.unlink(root / "empty.txt")
    os.rename(root / "a.txt", root / "改名后.txt")
    expected = snapshot(root)
    _lock(root, audit_dir)
    assert len(list(_blob_dir(root).iterdir())) == 5  # 6 - 删掉的 1 个；改名的换成新密文，旧的被清掉
    _unlock(root, audit_dir)
    assert snapshot(root) == expected


def test_mode_change_is_detected(unlocked):
    root, audit_dir = unlocked
    if os.name == "nt":
        pytest.skip("Windows 不还原权限位")
    os.chmod(root / "sub" / "readme.only", 0o644)
    expected = snapshot(root)
    _lock(root, audit_dir)
    _unlock(root, audit_dir)
    assert snapshot(root) == expected


@pytest.mark.parametrize("damage", ["overwrite", "delete"])
def test_kept_ciphertext_damaged_while_unlocked_falls_back_to_plaintext(unlocked, damage):
    """解锁期间 .vault 没有保护：保留的密文被改坏 / 删掉，上锁时以明文为准重新加密，不丢数据。"""
    root, audit_dir = unlocked
    expected = snapshot(root)
    for b in sorted(_blob_dir(root).iterdir())[:2]:
        if damage == "delete":
            os.unlink(b)
        else:
            b.write_bytes(b"garbage" * 10)
    _lock(root, audit_dir)
    v = Vault(root, audit_dir=audit_dir)
    assert v.verify(password=PASSWORD)["ok"]
    v.close()
    _unlock(root, audit_dir)
    assert snapshot(root) == expected


def test_low_space_mode_deletes_ciphertext_like_before(tmp_path, audit_dir, monkeypatch):
    monkeypatch.setenv("FDVAULT_KEEP_CIPHERTEXT", "0")
    root = tmp_path / "data"
    make_tree(root)
    before = snapshot(root)
    v = Vault(root, audit_dir=audit_dir)
    v.create(PASSWORD, QUESTIONS)
    v.close()
    _unlock(root, audit_dir)
    assert not list(_blob_dir(root).iterdir())
    assert _kept_rows(root) == 0
    _lock(root, audit_dir)
    _unlock(root, audit_dir)
    assert snapshot(root) == before


def test_not_enough_space_for_both_copies_falls_back(unlocked, monkeypatch):
    root, audit_dir = unlocked
    _lock(root, audit_dir)
    from core import fsutil

    monkeypatch.setattr(fsutil, "free_bytes", lambda _p: 300 * 1024 * 1024)
    _unlock(root, audit_dir)  # 剩 300 MB < 全部明文 + 1 GB 余量 → 不保留密文
    assert not list(_blob_dir(root).iterdir())


def test_python_engine_reads_native_fingerprints(unlocked, monkeypatch):
    """C++ 记的指纹，Python 兜底实现也认（POSIX 上两边字段一致）。"""
    if os.name == "nt":
        pytest.skip("Windows 的 Python 兜底不提供指纹")
    root, audit_dir = unlocked
    blobs = _blob_ids(root)
    monkeypatch.setenv("FDVAULT_ENGINE", "python")
    _lock(root, audit_dir)
    assert _blob_ids(root) == blobs
