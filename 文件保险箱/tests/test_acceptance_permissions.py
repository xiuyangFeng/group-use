"""验收第 13 条的自动化部分：保险箱目录/文件权限不能被同机其他账户读。

真正的"另一个系统账户打不开"还得真机建账户验证；这里先保证权限位是对的。
"""

from __future__ import annotations

import os

import pytest

from core import constants as C
from core.vault import Vault
from tests.conftest import PASSWORD, make_tree


def _mode(path) -> int:
    return path.stat().st_mode & 0o777


@pytest.mark.skipif(os.name != "posix", reason="Windows 用 ACL，这里只验 POSIX 权限位")
def test_vault_directories_and_files_are_private(tmp_path):
    root = tmp_path / "权限测试"
    make_tree(root)
    v = Vault(root, audit_dir=tmp_path / "audit")
    v.create(PASSWORD)
    v.close()

    assert _mode(root / C.VAULT_DIRNAME) == 0o700
    assert _mode(root / C.VAULT_DIRNAME / "blobs") == 0o700
    assert _mode(root / C.VAULT_DIRNAME / "tmp") == 0o700
    assert _mode(root / C.VAULT_DIRNAME / "header.json") == 0o600
    assert _mode(root / C.VAULT_DIRNAME / "manifest.db") == 0o600
    audit = tmp_path / "audit" / "audit.jsonl"
    if audit.exists():
        assert _mode(audit) == 0o600

    # 根目录下的说明文件是故意公开的，里面不能有密码/密钥
    marker = root / C.MARKER_NAME
    assert marker.exists()
    text = marker.read_text(encoding="utf-8")
    assert "密码" in text and "恢复密钥" in text
    for secret in ("argon2", "vault_key", "header"):
        assert secret not in text


@pytest.mark.skipif(os.name != "posix", reason="Windows 用 ACL")
def test_unlocked_plaintext_keeps_original_mode(tmp_path):
    root = tmp_path / "权限还原"
    make_tree(root)
    target = root / "脚本.sh"
    target.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    target.chmod(0o640)
    before = _mode(target)

    v = Vault(root, audit_dir=tmp_path / "audit")
    v.create(PASSWORD)
    v.close()
    v = Vault(root, audit_dir=tmp_path / "audit")
    v.unlock(PASSWORD)
    v.close()
    assert _mode(target) == before
