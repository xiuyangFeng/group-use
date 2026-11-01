"""header.json 的读写：明文外壳，不含任何秘密（密钥都是包裹后的密文）。"""

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

    # ---------------------------------------------------------------- 序列化

    def to_dict(self) -> dict:
        return {
            "format": C.FORMAT,
            "version": C.FORMAT_VERSION,
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
                wrapped_rec=dict(data["wrapped_vault_key_recovery"]),
                recovery_fingerprint=str(data["recovery_fingerprint"]),
                recovery_generated_at=str(data["recovery_generated_at"]),
                state=str(data["state"]),
                state_since=str(data["state_since"]),
                file_count=int(data.get("file_count", 0)),
                total_bytes=int(data.get("total_bytes", 0)),
                failed_attempts=int(data.get("failed_attempts", 0)),
                last_failed_at=str(data.get("last_failed_at", "")),
            )
        except KeyError as exc:
            raise NotAVaultError(f"header.json 缺少字段：{exc}")

    # ---------------------------------------------------------------- 便捷方法

    @staticmethod
    def new(kdf: dict, wrapped_pw: dict, wrapped_rec: dict, rec_fp: str) -> "Header":
        ts = now_iso()
        return Header(
            vault_id=str(uuid.uuid4()),
            created_at=ts,
            kdf=kdf,
            wrapped_pw=wrapped_pw,
            wrapped_rec=wrapped_rec,
            recovery_fingerprint=rec_fp,
            recovery_generated_at=ts,
            state=C.LOCKING,
            state_since=ts,
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
