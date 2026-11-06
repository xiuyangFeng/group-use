"""密码学原语：Argon2id 口令派生、HKDF、AEAD 包裹、恢复密钥。

设计（详见 docs/技术方案.md §3）：
  用户密码 --Argon2id--> KEK_pw --AEAD 解包--> vault_key K (32B 随机)
  恢复密钥 R --HKDF--> KEK_rec --AEAD 解包--> K
  K --HKDF--> 每文件子密钥 / 文件名密钥
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
from dataclasses import dataclass

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .errors import PasswordError, RecoveryKeyError

KEY_LEN = 32
SALT_LEN = 16
RECOVERY_KEY_LEN = 20  # 160 bit -> Base32 32 字符
RECOVERY_AAD_PREFIX = b"fdv1 recovery"
B32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


# ---------------------------------------------------------------- 编码

def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


# ---------------------------------------------------------------- KDF

DEFAULT_KDF = {"name": "argon2id", "m": 65536, "t": 3, "p": 1}


def derive_kek(password: str, salt: bytes, kdf: dict) -> bytes:
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=int(kdf.get("t", 3)),
        memory_cost=int(kdf.get("m", 65536)),
        parallelism=int(kdf.get("p", 1)),
        hash_len=KEY_LEN,
        type=Type.ID,
    )


def choose_kdf_params(target_seconds: float = 0.7) -> dict:
    """按机器速度校准 Argon2id 迭代次数，写入 header 以便以后能解开。"""
    params = dict(DEFAULT_KDF)
    if os.environ.get("FDVAULT_KDF_FAST") == "1":
        # 只给自动化测试用：参数会写进 header，正式数据不要用
        params.update({"m": 8192, "t": 1})
        return params
    salt = os.urandom(SALT_LEN)
    # 至少跑一次，避免创建保险箱时用户等太久
    start = time.perf_counter()
    derive_kek("calibration", salt, params)
    elapsed = time.perf_counter() - start
    while elapsed * (params["t"] + 1) / params["t"] < target_seconds and params["t"] < 8:
        params["t"] += 1
        start = time.perf_counter()
        derive_kek("calibration", salt, params)
        elapsed = time.perf_counter() - start
    return params


# ---------------------------------------------------------------- KDF 密码检查

def check_password_length(password: str) -> None:
    # 长度检查由调用方决定（CLI/GUI 用 core.passwords），这里只做最低限度的空值检查
    if not password:
        raise PasswordError("密码不能为空")


# ---------------------------------------------------------------- AEAD

def aead_seal(key: bytes, plaintext: bytes, aad: bytes = b"") -> tuple[bytes, bytes]:
    nonce = os.urandom(12)
    return nonce, ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad or None)


def aead_open(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes = b"") -> bytes:
    return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, aad or None)


def wrap_keys(vault_key: bytes, kek: bytes, vault_id: str, purpose: str) -> dict:
    aad = f"fdv1|{vault_id}|{purpose}".encode("utf-8")
    nonce, ct = aead_seal(kek, vault_key, aad)
    return {"n": b64e(nonce), "c": b64e(ct)}


def unwrap_keys(wrapped: dict, kek: bytes, vault_id: str, purpose: str) -> bytes:
    aad = f"fdv1|{vault_id}|{purpose}".encode("utf-8")
    try:
        return aead_open(kek, b64d(wrapped["n"]), b64d(wrapped["c"]), aad)
    except InvalidTag:
        raise


# ---------------------------------------------------------------- HKDF

def hkdf(ikm: bytes, info: bytes, salt: bytes = b"", length: int = KEY_LEN) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt or None, info=info).derive(ikm)


def file_key(vault_key: bytes, uuid: bytes) -> bytes:
    return hkdf(vault_key, info=b"fdv1 file key", salt=uuid)


def name_key(vault_key: bytes) -> bytes:
    return hkdf(vault_key, info=b"fdv1 name key")


# ---------------------------------------------------------------- 恢复密钥

def new_recovery_key() -> bytes:
    return os.urandom(RECOVERY_KEY_LEN)


def recovery_key_str(key: bytes) -> str:
    s = base64.b32encode(key).decode("ascii").rstrip("=")
    groups = [s[i : i + 4] for i in range(0, len(s), 4)]
    return "-".join(groups)


def recovery_key_parse(text: str) -> bytes:
    cleaned = "".join(ch for ch in text.upper() if ch.isalnum())
    cleaned = "".join(ch for ch in cleaned if ch in B32_ALPHABET)
    if not cleaned:
        raise RecoveryKeyError("恢复密钥为空")
    pad = (-len(cleaned)) % 8
    try:
        raw = base64.b32decode(cleaned + "=" * pad)
    except Exception:
        raise RecoveryKeyError("恢复密钥格式不对")
    if len(raw) != RECOVERY_KEY_LEN:
        raise RecoveryKeyError("恢复密钥长度不对")
    return raw


def recovery_kek(key: bytes, vault_id: str) -> bytes:
    return hkdf(key, info=b"fdv1 recovery kek", salt=vault_id.encode("utf-8"))


def recovery_fingerprint(key: bytes) -> str:
    s = recovery_key_str(key)
    parts = s.split("-")
    return f"{parts[0]}-…-{parts[-1]}"


# ---------------------------------------------------------------- 杂项

def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def new_uuid() -> bytes:
    return os.urandom(16)
