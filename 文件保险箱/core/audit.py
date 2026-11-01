"""审计日志：JSONL + 哈希链，记录谁在什么时候解锁/上锁了哪个保险箱。

诚实说明（写进导出文件）：
- 每人一个系统账户后，actor 自动取当前系统账户名，可信度较高；
- 但密码是共用的，所以只能证明「谁在操作」，不能证明「谁有权操作」；
- 本机管理员可以重写整条链，哈希链只能发现简单篡改，不是防篡改证据。
"""

from __future__ import annotations

import csv
import getpass
import hashlib
import json
import os
import socket
import time
from pathlib import Path

from . import __version__

WEAK_AUDIT_NOTE = (
    "说明：本日志记录系统账户名与操作时间；由于采用组内共用密码，"
    "只能证明谁在操作，不能证明谁有权操作；本机管理员可修改日志，"
    "哈希链仅用于发现简单篡改，不能作为严格合规证据。"
)


def _canonical(rec: dict) -> bytes:
    return json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash_record(rec: dict) -> str:
    return hashlib.sha256(_canonical(rec)).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 写

    def _last_hash(self) -> str:
        if not self.path.exists():
            return "0" * 64
        try:
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 8192))
                tail = f.read().decode("utf-8", "replace").strip().splitlines()
            if not tail:
                return "0" * 64
            return str(json.loads(tail[-1]).get("hash") or "0" * 64)
        except Exception:
            return "0" * 64

    def record(
        self,
        event: str,
        *,
        vault_path: str = "",
        vault_id: str = "",
        result: str = "ok",
        detail: str = "",
        actor: str | None = None,
    ) -> dict:
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": event,
            "actor": actor or self._current_actor(),
            "vault_path": vault_path,
            "vault_id": vault_id,
            "result": result,
            "detail": detail,
            "host": socket.gethostname(),
            "app_version": __version__,
            "prev_hash": self._last_hash(),
        }
        rec["hash"] = _hash_record(rec)
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return rec

    @staticmethod
    def _current_actor() -> str:
        try:
            return getpass.getuser()
        except Exception:
            return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"

    # ---------------------------------------------------------------- 读 / 校验

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        out.append({"broken": True, "raw": line[:200]})
        return out

    def verify(self) -> tuple[bool, int, str]:
        """返回 (是否完整, 记录数, 说明)。"""
        recs = self.records()
        prev = "0" * 64
        for idx, rec in enumerate(recs, 1):
            if rec.get("broken"):
                return False, len(recs), f"第 {idx} 行不是合法 JSON"
            stored = rec.get("hash")
            body = {k: v for k, v in rec.items() if k != "hash"}
            if rec.get("prev_hash") != prev:
                return False, len(recs), f"第 {idx} 行的前序哈希不匹配"
            if _hash_record(body) != stored:
                return False, len(recs), f"第 {idx} 行的内容被改过"
            prev = stored
        return True, len(recs), "完整"

    def export_csv(self, out_path: str | Path) -> Path:
        out = Path(out_path)
        recs = self.records()
        with open(out, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["时间", "事件", "操作人（系统账户）", "保险箱", "结果", "详情", "主机", "软件版本", "校验值"])
            for rec in recs:
                writer.writerow(
                    [
                        rec.get("ts", ""),
                        rec.get("event", ""),
                        rec.get("actor", ""),
                        rec.get("vault_path", ""),
                        rec.get("result", ""),
                        rec.get("detail", ""),
                        rec.get("host", ""),
                        rec.get("app_version", ""),
                        rec.get("hash", ""),
                    ]
                )
            writer.writerow([])
            writer.writerow([WEAK_AUDIT_NOTE])
        try:
            os.chmod(out, 0o600)
        except OSError:
            pass
        return out
