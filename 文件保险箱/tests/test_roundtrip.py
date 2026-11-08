"""端到端：创建 / 解锁 / 上锁 / 改文件后再上锁。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core import constants as C
from core.errors import IntegrityError, PasswordError, VaultError
from core import protect
from core.vault import Vault
from tests.conftest import PASSWORD, QUESTIONS, make_tree, snapshot, write_file


def open_vault(root: Path, audit_dir: Path) -> Vault:
    return Vault(root, audit_dir=audit_dir)


def create_vault(root: Path, audit_dir: Path) -> dict:
    v = open_vault(root, audit_dir)
    res = v.create(PASSWORD, QUESTIONS)
    assert res["questions"] == [q for q, _a in QUESTIONS]
    v.close()
    return res


def test_create_locks_everything(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    files_before = [p for p in root.rglob("*") if p.is_file()]
    create_vault(root, audit_dir)

    remaining = {p.name for p in root.iterdir()}
    assert remaining == {".vault", "如何解锁.txt"}
    assert (root / "如何解锁.txt").exists()
    assert not (root / "a.txt").exists()

    blob_dir = root / ".vault" / "blobs"
    blobs = list(blob_dir.iterdir())
    assert len(blobs) == 6  # a.txt / 中文文件.txt / empty.txt / sub 下 3 个
    assert all(b.suffix == ".blob" for b in blobs)

    # 密文 / manifest / journal 里不能出现明文文件名
    blobs_and_db = [p for p in (root / ".vault").rglob("*") if p.is_file()]
    raw = b"".join(p.read_bytes() for p in blobs_and_db)
    for name in ["a.txt", "big.bin", "中文文件", "空格 和#号"]:
        assert name.encode("utf-8") not in raw
    # 也不能出现明文内容
    assert b"hello world" not in raw

    assert Vault(root).status()["state"] == C.LOCKED
    assert len(files_before) == 6


def test_unlock_restores_bytes_and_metadata(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    before = snapshot(root)
    create_vault(root, audit_dir)

    v = open_vault(root, audit_dir)
    stats = v.unlock(password=PASSWORD)
    v.close()
    assert stats["files"] == 6
    assert Vault(root).status()["state"] == C.UNLOCKED
    assert snapshot(root) == before
    # 空间够时解锁保留密文（下次上锁没改过的文件不用重新加密）
    assert len(list((root / ".vault" / "blobs").iterdir())) == 6


def test_repeated_cycles(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    before = snapshot(root)
    create_vault(root, audit_dir)
    for _ in range(3):
        v = open_vault(root, audit_dir)
        v.unlock(password=PASSWORD)
        assert snapshot(root) == before
        v.lock(password=PASSWORD)
        assert snapshot(root) == {}
        v.close()
        assert Vault(root).status()["state"] == C.LOCKED


def test_modify_add_delete_rename_while_unlocked(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    create_vault(root, audit_dir)

    v = open_vault(root, audit_dir)
    v.unlock(password=PASSWORD)
    (root / "a.txt").write_bytes(b"changed content\n")
    (root / "new" / "added.bin").parent.mkdir(exist_ok=True)
    (root / "new" / "added.bin").write_bytes(os.urandom(150_000))
    os.unlink(root / "sub" / "readme.only")
    os.rename(root / "中文文件.txt", root / "改名后.txt")
    os.rmdir(root / "空目录")
    expected = snapshot(root)
    v.lock(password=PASSWORD)
    v.close()

    v = open_vault(root, audit_dir)
    v.unlock(password=PASSWORD)
    v.close()
    assert snapshot(root) == expected


def test_empty_file_and_empty_dirs(tmp_path, audit_dir):
    root = tmp_path / "data"
    root.mkdir()
    (root / "empty.txt").write_bytes(b"")
    (root / "空目录").mkdir()
    create_vault(root, audit_dir)

    v = open_vault(root, audit_dir)
    v.unlock(password=PASSWORD)
    v.close()
    assert (root / "empty.txt").read_bytes() == b""
    assert (root / "empty.txt").stat().st_size == 0
    assert (root / "空目录").is_dir()


def test_wrong_password_keeps_locked_and_audits(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    create_vault(root, audit_dir)

    v = open_vault(root, audit_dir)
    with pytest.raises(PasswordError):
        v.unlock(password="wrong-password-9876")
    v.close()
    assert Vault(root).status()["state"] == C.LOCKED

    from core.audit import AuditLog

    log = AuditLog(audit_dir / "audit.jsonl")
    events = [r["event"] for r in log.records()]
    assert "UNLOCK_FAILED" in events


def test_verify_catches_missing_blob(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    create_vault(root, audit_dir)

    protect.release(root)  # 模拟绕过防删除保护
    blobs = sorted((root / ".vault" / "blobs").iterdir())
    os.unlink(blobs[0])

    v = open_vault(root, audit_dir)
    result = v.verify(password=PASSWORD)
    v.close()
    assert not result["ok"]
    assert len(result["problems"]) == 1


def test_unlock_refuses_when_blob_missing(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    create_vault(root, audit_dir)
    protect.release(root)  # 模拟绕过防删除保护
    os.unlink(sorted((root / ".vault" / "blobs").iterdir())[0])

    v = open_vault(root, audit_dir)
    with pytest.raises(IntegrityError):
        v.unlock(password=PASSWORD)
    v.close()
    assert Vault(root).status()["state"] == C.LOCKED


def test_short_password_rejected(tmp_path, audit_dir):
    """密码规则：正好 6 位数字；不合格的不能建库。"""
    from core import passwords

    root = tmp_path / "data"
    root.mkdir()
    (root / "a.txt").write_bytes(b"x")
    # 合法样例（规则函数本身也要对）
    assert passwords.password_problems("123456") == []
    for bad in ("12345", "1234567", "Abcdef12", "12345a", "", "12 456"):
        v = open_vault(root, audit_dir)
        with pytest.raises(VaultError) as exc:
            v.create(bad, QUESTIONS)
        assert "不符合要求" in str(exc.value)
        assert not (root / ".vault").exists()


def test_create_twice_rejected(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    create_vault(root, audit_dir)
    v = open_vault(root, audit_dir)
    with pytest.raises(VaultError):
        v.create(PASSWORD, QUESTIONS)


def test_state_machine_rejects_wrong_ops(tmp_path, audit_dir):
    from core.errors import AlreadyVaultError, VaultStateError

    root = tmp_path / "data"
    make_tree(root)
    create_vault(root, audit_dir)

    v = open_vault(root, audit_dir)
    with pytest.raises(VaultStateError):
        v.lock(password=PASSWORD)  # 已经锁着
    with pytest.raises(AlreadyVaultError):
        v.create(PASSWORD, QUESTIONS)  # 已经存在，不能重复创建
    v.close()


def test_verify_ok(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    create_vault(root, audit_dir)
    v = open_vault(root, audit_dir)
    result = v.verify(password=PASSWORD)
    v.close()
    assert result["ok"] and result["files"] == 6 and result["problems"] == []


def test_weird_names_and_long_paths(tmp_path, audit_dir):
    """验收第 8 条：中文、空格、emoji、超长文件名/深层路径、空文件夹都要能还原。"""
    root = tmp_path / "边界名字"
    make_tree(root)
    write_file(root / "名字" / "中文 空格 emoji 😀🎉.txt", b"x")
    write_file(root / "名字" / ("很长的文件名" * 18 + ".txt"), b"long name")
    write_file(root / "名字" / ("a" * 200 + ".bin"), b"200 ascii")
    deep = root
    for i in range(12):
        deep = deep / f"第{i+1}层目录"
    write_file(deep / "深路径文件.txt", b"deep")
    (root / "空目录" / "再空一层").mkdir(parents=True)

    before = snapshot(root)
    v = Vault(root, audit_dir=audit_dir)
    v.create(PASSWORD, QUESTIONS)
    v.close()
    v = Vault(root, audit_dir=audit_dir)
    v.unlock(PASSWORD)
    v.close()
    assert snapshot(root) == before
