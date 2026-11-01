"""单文件的分块加解密。

密文格式（blob）：
    magic "FDB1" | version(1) | chunk_size(4, LE) | plaintext_size(8, LE) | chunk_count(8, LE)
    | chunk_0 | chunk_1 | ...

每个 chunk 用「文件子密钥 + 计数器 nonce + AAD(文件 uuid, 块号, 是否末块)」加密，
因此密文块被重排、截断、跨文件拼接都会被发现。
"""

from __future__ import annotations

import hashlib
import os
import struct
from typing import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from . import constants as C
from .errors import IntegrityError
from .fsutil import long_path

MAGIC = b"FDB1"
VERSION = 1
HEADER = struct.Struct("<4sBIQQ")
TAG_LEN = 16

ProgressFn = Callable[[int], None]


def _nonce(index: int) -> bytes:
    return b"\x00" * 4 + index.to_bytes(8, "big")


def _aad(file_uuid: bytes, index: int, final: bool) -> bytes:
    return b"fdv1|chunk|" + file_uuid + index.to_bytes(8, "big") + (b"\x01" if final else b"\x00")


def _chunk_plan(size: int, chunk_size: int) -> tuple[int, list[int]]:
    count = (size + chunk_size - 1) // chunk_size
    return count, [chunk_size] * max(0, count - 1)


def encrypt_file(
    src: str | os.PathLike,
    dst: str | os.PathLike,
    file_key: bytes,
    file_uuid: bytes,
    chunk_size: int = C.DEFAULT_CHUNK_SIZE,
    progress: ProgressFn | None = None,
) -> tuple[int, bytes]:
    """把 src 加密写到 dst（调用方负责 tmp+rename）。返回 (明文大小, 明文 sha256)。"""
    total = os.path.getsize(long_path(src))
    count = (total + chunk_size - 1) // chunk_size
    aead = ChaCha20Poly1305(file_key)
    h = hashlib.sha256()

    with open(long_path(src), "rb") as fi, open(long_path(dst), "wb") as fo:
        fo.write(HEADER.pack(MAGIC, VERSION, chunk_size, total, count))
        for idx in range(count):
            data = fi.read(chunk_size)
            if not data:
                raise IntegrityError(f"读取 {src} 时意外结束")
            h.update(data)
            ct = aead.encrypt(_nonce(idx), data, _aad(file_uuid, idx, idx == count - 1))
            fo.write(ct)
            if progress:
                progress(len(data))
        fo.flush()
        os.fsync(fo.fileno())

    if os.path.getsize(long_path(dst)) != HEADER.size + total + count * TAG_LEN:
        raise IntegrityError("加密结果大小不对")
    return total, h.digest()


def _read_header(fh) -> tuple[int, int, int]:
    raw = fh.read(HEADER.size)
    if len(raw) != HEADER.size:
        raise IntegrityError("密文文件头不完整")
    magic, version, chunk_size, plain_size, count = HEADER.unpack(raw)
    if magic != MAGIC or version != VERSION:
        raise IntegrityError("密文格式不对（可能文件损坏）")
    if chunk_size <= 0:
        raise IntegrityError("密文头部的块大小不合法")
    return chunk_size, plain_size, count


def _decrypt_stream(src, file_key: bytes, file_uuid: bytes, expected_size: int | None, sink, progress: ProgressFn | None = None) -> tuple[int, bytes]:
    aead = ChaCha20Poly1305(file_key)
    h = hashlib.sha256()
    with open(long_path(src), "rb") as fh:
        chunk_size, plain_size, count = _read_header(fh)
        if expected_size is not None and plain_size != expected_size:
            raise IntegrityError("密文大小与记录不一致")
        expected_count = (plain_size + chunk_size - 1) // chunk_size
        if count != expected_count:
            raise IntegrityError("密文块数不对")
        done = 0
        for idx in range(count):
            plain_len = chunk_size if idx < count - 1 else plain_size - chunk_size * (count - 1)
            ct = fh.read(plain_len + TAG_LEN)
            if len(ct) != plain_len + TAG_LEN:
                raise IntegrityError("密文被截断")
            try:
                data = aead.decrypt(_nonce(idx), ct, _aad(file_uuid, idx, idx == count - 1))
            except InvalidTag:
                raise IntegrityError("密文校验失败（文件损坏或被篡改）")
            h.update(data)
            if sink is not None:
                sink.write(data)
            done += len(data)
            if progress:
                progress(len(data))
        if done != plain_size:
            raise IntegrityError("解密后大小不对")
        if fh.read(1):
            raise IntegrityError("密文尾部有多余数据")
    return plain_size, h.digest()


def decrypt_file(
    src: str | os.PathLike,
    dst: str | os.PathLike,
    file_key: bytes,
    file_uuid: bytes,
    expected_size: int | None,
    expected_sha256: bytes,
    progress: ProgressFn | None = None,
) -> bytes:
    """解密到 dst（调用方负责 tmp+rename），返回明文 sha256。"""
    with open(long_path(dst), "wb") as fo:
        _, digest = _decrypt_stream(src, file_key, file_uuid, expected_size, fo, progress)
        fo.flush()
        os.fsync(fo.fileno())
    if expected_sha256 and digest != expected_sha256:
        raise IntegrityError("解密后哈希与记录不一致（数据可能已损坏）")
    return digest


def verify_blob(
    src: str | os.PathLike,
    file_key: bytes,
    file_uuid: bytes,
    expected_size: int | None = None,
    progress: ProgressFn | None = None,
) -> tuple[int, bytes]:
    """只解密不落盘，用于校验/恢复。返回 (明文大小, 明文 sha256)。"""
    size = expected_size if expected_size is not None else 0
    if expected_size is None:
        with open(long_path(src), "rb") as fh:
            _, size, _ = _read_header(fh)
    return _decrypt_stream(src, file_key, file_uuid, size, None, progress)
