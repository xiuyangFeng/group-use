"""外部程序"正在使用"标记（CT 脱敏工具运行期间写在 .vault 里）：上锁要拦住，过期/结束的标记不算。"""

from __future__ import annotations

import json
import time

import pytest

from core import constants as C
from core import inuse
from core.errors import InUseError
from core.vault import Vault
from tests.conftest import PASSWORD, QUESTIONS, make_tree, snapshot


def _unlocked_vault(tmp_path):
    root = tmp_path / "原始数据"
    make_tree(root)
    v = Vault(root, audit_dir=tmp_path / "audit")
    v.create(PASSWORD, QUESTIONS)
    v.close()
    v = Vault(root, audit_dir=tmp_path / "audit")
    v.unlock(PASSWORD)
    return root, v


def _mark(root, *, active=True, age=0.0, name="in-use-ct-deid.json"):
    data = {"tool": "CT 数据脱敏工具", "active": active, "heartbeat": time.time() - age,
            "started_at": "2026-11-03T14:05:00+08:00", "what": "原始数据、映射表"}
    p = root / C.VAULT_DIRNAME / name
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_lock_refused_while_in_use(tmp_path):
    root, v = _unlocked_vault(tmp_path)
    _mark(root)
    before = snapshot(root)
    with pytest.raises(InUseError) as ei:
        v.lock()
    msg = str(ei.value)
    assert "CT 数据脱敏工具" in msg and "14:05 开始" in msg and "不能上锁" in msg
    assert Vault(root).status()["state"] == C.UNLOCKED, "拦下时不能改动状态"
    assert snapshot(root) == before, "拦下时不能动文件"
    v.close()


def test_lock_allowed_after_tool_finished(tmp_path):
    root, v = _unlocked_vault(tmp_path)
    marker = _mark(root, active=False)
    v.lock()
    v.close()
    assert Vault(root).status()["state"] == C.LOCKED
    # 标记在 .vault 里：不参与加密，解锁上锁后原样还在
    assert marker.exists() and json.loads(marker.read_text("utf-8"))["active"] is False
    v = Vault(root, audit_dir=tmp_path / "audit")
    v.unlock(PASSWORD)
    assert not (root / marker.name).exists(), "标记不能被当成用户文件解密出来"
    v.close()


def test_stale_marker_ignored(tmp_path):
    """脱敏工具崩溃后心跳不再刷新：超过时限就不再拦。"""
    root, v = _unlocked_vault(tmp_path)
    _mark(root, age=C.INUSE_STALE_SECONDS + 5)
    v.lock()
    v.close()
    assert Vault(root).status()["state"] == C.LOCKED


@pytest.mark.parametrize("content", ["不是 JSON", "[]", '{"active": true}', '{"active": "yes", "heartbeat": 0}'])
def test_broken_marker_ignored(tmp_path, content):
    (tmp_path / C.VAULT_DIRNAME).mkdir()
    (tmp_path / C.VAULT_DIRNAME / "in-use-x.json").write_text(content, encoding="utf-8")
    assert inuse.active_users(tmp_path) == []


def test_future_heartbeat_beyond_skew_ignored(tmp_path):
    (tmp_path / C.VAULT_DIRNAME).mkdir()
    _mark(tmp_path, age=-(C.INUSE_CLOCK_SKEW_SECONDS + 30))
    assert inuse.active_users(tmp_path) == []
    _mark(tmp_path, age=-5)
    assert len(inuse.active_users(tmp_path)) == 1


def test_status_reports_in_use(tmp_path):
    root, v = _unlocked_vault(tmp_path)
    assert v.status()["in_use_by"] == ""
    _mark(root)
    assert "CT 数据脱敏工具" in Vault(root).status()["in_use_by"]
    v.close()
