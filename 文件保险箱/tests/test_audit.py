"""审计日志测试。"""

from __future__ import annotations

import json

from core.audit import AuditLog
from core.vault import Vault
from tests.conftest import PASSWORD, make_tree


def test_chain_ok(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("CREATE", vault_path="/a", detail="x")
    log.record("UNLOCK", vault_path="/a")
    log.record("LOCK", vault_path="/a")
    ok, count, detail = log.verify()
    assert ok and count == 3, detail


def test_tamper_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record("CREATE", vault_path="/a")
    log.record("UNLOCK", vault_path="/a")

    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["result"] = "fail"
    rec["actor"] = "someone-else"
    lines[1] = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, count, detail = AuditLog(path).verify()
    assert not ok
    assert "改过" in detail


def test_deleted_line_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(3):
        log.record("EVENT", detail=str(i))
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
    ok, _, detail = AuditLog(path).verify()
    assert not ok


def test_export_csv(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("CREATE", vault_path="/a", detail="创建")
    out = log.export_csv(tmp_path / "audit.csv")
    # Windows 的 Excel 认 BOM 才会把中文当 UTF-8，不能省
    assert out.read_bytes().startswith(b"\xef\xbb\xbf")
    text = out.read_text(encoding="utf-8-sig")
    assert "操作人（系统账户）" in text
    assert "只能证明谁在操作" in text  # 导出文件里必须写明局限
    assert "创建" in text


def test_vault_records_events(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    v = Vault(root, audit_dir=audit_dir)
    v.create(PASSWORD)
    v.unlock(password=PASSWORD)
    v.lock(password=PASSWORD)
    v.verify(password=PASSWORD)
    v.close()

    log = AuditLog(audit_dir / "audit.jsonl")
    events = [r["event"] for r in log.records()]
    for expected in ["CREATE", "UNLOCK", "LOCK", "VERIFY"]:
        assert expected in events
    ok, count, detail = log.verify()
    assert ok, detail
    # 每条记录都有系统账户名
    assert all(r.get("actor") for r in log.records())
