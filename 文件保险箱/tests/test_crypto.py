"""密码学原语测试。"""

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag

from core import crypto


def test_recovery_key_roundtrip():
    key = crypto.new_recovery_key()
    assert len(key) == 20
    s = crypto.recovery_key_str(key)
    assert len(s.replace("-", "")) == 32
    assert crypto.recovery_key_parse(s) == key


def test_recovery_key_parse_tolerant():
    key = crypto.new_recovery_key()
    s = crypto.recovery_key_str(key)
    variants = [s.lower(), s.replace("-", " "), " " + s + " ", s.replace("-", "")]
    for v in variants:
        assert crypto.recovery_key_parse(v) == key


@pytest.mark.parametrize("bad", ["", "abc", "!!!!", "A" * 20, "A" * 40])
def test_recovery_key_parse_rejects(bad):
    with pytest.raises(Exception):
        crypto.recovery_key_parse(bad)


def test_recovery_key_parse_accepts_wrong_but_valid_key():
    # 语法合法但内容不对的密钥，解析应该成功；校验失败发生在解锁时
    key = crypto.recovery_key_parse("A" * 32)
    assert len(key) == 20


def test_wrap_unwrap():
    key = os.urandom(32)
    kek = os.urandom(32)
    wrapped = crypto.wrap_keys(key, kek, "vault-id-1", "password")
    assert crypto.unwrap_keys(wrapped, kek, "vault-id-1", "password") == key
    with pytest.raises(InvalidTag):
        crypto.unwrap_keys(wrapped, kek, "vault-id-2", "password")
    with pytest.raises(InvalidTag):
        crypto.unwrap_keys(wrapped, os.urandom(32), "vault-id-1", "password")
    with pytest.raises(InvalidTag):
        crypto.unwrap_keys(wrapped, kek, "vault-id-1", "recovery")


def test_aead_aad_binding():
    key = os.urandom(32)
    nonce, ct = crypto.aead_seal(key, b"secret", b"aad-1")
    assert crypto.aead_open(key, nonce, ct, b"aad-1") == b"secret"
    with pytest.raises(InvalidTag):
        crypto.aead_open(key, nonce, ct, b"aad-2")


def test_kdf_deterministic():
    salt = os.urandom(16)
    params = {"name": "argon2id", "m": 8192, "t": 1, "p": 1}
    a = crypto.derive_kek("password", salt, params)
    b = crypto.derive_kek("password", salt, params)
    c = crypto.derive_kek("password2", salt, params)
    assert a == b and a != c and len(a) == 32


def test_hkdf_domain_separation():
    key = os.urandom(32)
    uuid = os.urandom(16)
    assert crypto.file_key(key, uuid) != crypto.name_key(key)
    assert crypto.file_key(key, uuid) != crypto.file_key(key, os.urandom(16))


def test_fingerprint_format():
    import re

    key = crypto.new_recovery_key()
    fp = crypto.recovery_fingerprint(key)
    assert re.fullmatch(r"[A-Z2-7]{4}-…-[A-Z2-7]{4}", fp), fp
    # 指纹要能对上恢复密钥的首尾
    s = crypto.recovery_key_str(key)
    assert fp.split("-…-")[0] == s.split("-")[0]
    assert fp.split("-…-")[1] == s.split("-")[-1]
