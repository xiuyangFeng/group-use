"""header.json 的读写：明文外壳，不含任何秘密（密钥都是包裹后的密文；密保只存问题，不存答案）。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import constants as C
from . import crypto
from .errors import NotAVaultError
from .fsutil import atomic_write_json, read_json


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class Header:
    vault_id: str
    created_at: str
    kdf: dict
    wrapped_pw: dict
    wrapped_rec: dict
    recovery_fingerprint: str
    recovery_generated_at: str
    state: str
    state_since: str
    file_count: int = 0
    total_bytes: int = 0
    failed_attempts: int = 0
    last_failed_at: str = ""
    # 密保问题：[{"q": 问题原文, "kind": 答案类型, "kdf": {..., "salt"}, "wrapped": {"n", "c"}}]
    questions: list = field(default_factory=list)

    @property
    def has_recovery_key(self) -> bool:
        return bool(self.wrapped_rec)

    @property
    def question_texts(self) -> list[str]:
        return [str(q.get("q", "")) for q in self.questions]

    # ---------------------------------------------------------------- 序列化

    def to_dict(self) -> dict:
        # 还是「恢复密钥、没有密保」的老样子就继续写版本 1，旧版软件也能打开
        legacy = self.has_recovery_key and not self.questions
        data = {
            "format": C.FORMAT,
            "version": C.FORMAT_VERSION_LEGACY if legacy else C.FORMAT_VERSION,
            "vault_id": self.vault_id,
            "created_at": self.created_at,
            "kdf": self.kdf,
            "wrapped_vault_key_password": self.wrapped_pw,
            "wrapped_vault_key_recovery": self.wrapped_rec,
            "recovery_fingerprint": self.recovery_fingerprint,
            "recovery_generated_at": self.recovery_generated_at,
            "state": self.state,
            "state_since": self.state_since,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "failed_attempts": self.failed_attempts,
            "last_failed_at": self.last_failed_at,
        }
        if self.questions:
            data["security_questions"] = self.questions
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Header":
        try:
            if data.get("format") != C.FORMAT:
                raise NotAVaultError(f"不认识的保险箱格式：{data.get('format')!r}")
            if int(data.get("version", 0)) > C.FORMAT_VERSION:
                raise NotAVaultError("这个保险箱是更新版本的软件创建的，请升级软件")
            return cls(
                vault_id=str(data["vault_id"]),
                created_at=str(data["created_at"]),
                kdf=dict(data["kdf"]),
                wrapped_pw=dict(data["wrapped_vault_key_password"]),
                wrapped_rec=dict(data.get("wrapped_vault_key_recovery") or {}),
                recovery_fingerprint=str(data.get("recovery_fingerprint") or ""),
                recovery_generated_at=str(data.get("recovery_generated_at") or ""),
                state=str(data["state"]),
                state_since=str(data["state_since"]),
                file_count=int(data.get("file_count", 0)),
                total_bytes=int(data.get("total_bytes", 0)),
                failed_attempts=int(data.get("failed_attempts", 0)),
                last_failed_at=str(data.get("last_failed_at", "")),
                questions=[dict(q) for q in (data.get("security_questions") or [])],
            )
        except KeyError as exc:
            raise NotAVaultError(f"header.json 缺少字段：{exc}")

    # ---------------------------------------------------------------- 便捷方法

    @staticmethod
    def new(kdf: dict, wrapped_pw: dict, questions: list) -> "Header":
        ts = now_iso()
        return Header(
            vault_id=str(uuid.uuid4()),
            created_at=ts,
            kdf=kdf,
            wrapped_pw=wrapped_pw,
            wrapped_rec={},
            recovery_fingerprint="",
            recovery_generated_at="",
            state=C.LOCKING,
            state_since=ts,
            questions=questions,
        )

    @property
    def kdf_salt(self) -> bytes:
        return crypto.b64d(self.kdf["salt"])

    def set_state(self, state: str) -> None:
        self.state = state
        self.state_since = now_iso()


def load_header(header_path: str | Path) -> Header:
    p = Path(header_path)
    if not p.exists():
        raise NotAVaultError(f"不是保险箱：找不到 {p.name}")
    try:
        data = read_json(p)
    except Exception as exc:
        raise NotAVaultError(f"header.json 读不出来：{exc}")
    if not isinstance(data, dict):
        raise NotAVaultError("header.json 内容不对")
    return Header.from_dict(data)


def save_header(header_path: str | Path, header: Header) -> None:
    atomic_write_json(header_path, header.to_dict(), mode=0o600)
