"""单文件的分块加解密。

密文格式（blob）：
    magic "FDB1" | version(1) | chunk_size(4, LE) | plaintext_size(8, LE) | chunk_count(8, LE)
    | chunk_0 | chunk_1 | ...

每个 chunk 用「文件子密钥 + 计数器 nonce + AAD(文件 uuid, 块号, 是否末块)」加密，
因此密文块被重排、截断、跨文件拼接都会被发现。

实际加解密由 C++ 引擎完成（core/native.py → native/）：多文件并行、大文件按块并行。
本文件里的 _py_* 函数是同一格式的 Python 参考实现：C++ 引擎加载失败时兜底，
测试里也用它和 C++ 结果交叉比对。
"""

from __future__ import annotations

import hashlib
import os
import struct
import time
from dataclasses import dataclass
from typing import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from . import constants as C
from . import native
from .errors import Cancelled, IntegrityError, VaultError
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


def _py_encrypt_file(
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


def _py_decrypt_file(
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


def _py_verify_blob(
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


def _py_sha256_file(path: str | os.PathLike, progress: ProgressFn | None = None) -> bytes:
    h = hashlib.sha256()
    with open(long_path(path), "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
            if progress:
                progress(len(b))
    return h.digest()


# ================================================================ 对外接口（C++ 引擎优先）

BatchProgressFn = Callable[[int, int], None]  # (已处理字节, 已完成文件数)


@dataclass
class EncryptJob:
    src: str | os.PathLike
    dst: str | os.PathLike
    file_key: bytes
    file_uuid: bytes
    chunk_size: int = C.DEFAULT_CHUNK_SIZE
    size_hint: int | None = None


@dataclass
class DecryptJob:
    src: str | os.PathLike
    dst: str | os.PathLike | None  # None：只校验（verify）
    file_key: bytes
    file_uuid: bytes
    expected_size: int | None = None
    expected_sha256: bytes = b""


def engine_name() -> str:
    return "C++" if native.available() else "Python"


def _poll(control) -> None:
    """Python 兜底路径的暂停 / 取消（不计入 control.checked）。"""
    if control is None:
        return
    while control.paused and not control.cancelled:
        time.sleep(0.05)
    if control.cancelled:
        raise Cancelled("操作已取消")


class _PyProgress:
    def __init__(self, progress: BatchProgressFn | None) -> None:
        self.progress = progress
        self.bytes = 0
        self.files = 0

    def add(self, n: int) -> None:
        self.bytes += n
        if self.progress:
            self.progress(self.bytes, self.files)

    def file_done(self) -> None:
        self.files += 1
        if self.progress:
            self.progress(self.bytes, self.files)


def encrypt_many(
    jobs: list[EncryptJob], progress: BatchProgressFn | None = None, control=None
) -> list[tuple[int, bytes]]:
    """并行加密一批文件，返回每个文件的 (明文大小, 明文 sha256)。任何一个出错就抛异常（已写的 dst 由调用方清理）。"""
    if not jobs:
        return []
    if native.available():
        res = native.run(
            native.OP_ENCRYPT,
            [
                native.Job(
                    src=j.src, dst=j.dst, key=j.file_key, uuid=j.file_uuid,
                    chunk_size=j.chunk_size, expected_size=j.size_hint,
                )
                for j in jobs
            ],
            progress=progress,
            control=control,
        )
        return [(r.size, r.sha256) for r in res]
    pp = _PyProgress(progress)
    out = []
    for j in jobs:
        _poll(control)
        out.append(_py_encrypt_file(j.src, j.dst, j.file_key, j.file_uuid, j.chunk_size, pp.add))
        pp.file_done()
    return out


def decrypt_many(
    jobs: list[DecryptJob], progress: BatchProgressFn | None = None, control=None
) -> list[tuple[int, bytes]]:
    """并行解密一批文件到各自的 dst（调用方负责 tmp+rename），返回 (明文大小, 明文 sha256)。任何一个出错就抛异常。"""
    if not jobs:
        return []
    if native.available():
        res = native.run(
            native.OP_DECRYPT,
            [
                native.Job(
                    src=j.src, dst=j.dst, key=j.file_key, uuid=j.file_uuid,
                    expected_size=j.expected_size, expected_sha256=j.expected_sha256,
                )
                for j in jobs
            ],
            progress=progress,
            control=control,
        )
        return [(r.size, r.sha256) for r in res]
    pp = _PyProgress(progress)
    out = []
    for j in jobs:
        _poll(control)
        digest = _py_decrypt_file(
            j.src, j.dst, j.file_key, j.file_uuid, j.expected_size, j.expected_sha256, pp.add
        )
        size = os.path.getsize(long_path(j.dst))
        out.append((size, digest))
        pp.file_done()
    return out


def verify_many(
    jobs: list[DecryptJob], progress: BatchProgressFn | None = None, control=None
) -> list[tuple[int, bytes] | VaultError]:
    """并行校验一批密文（不落盘）。每项返回 (明文大小, sha256) 或完整性错误（不抛）；系统 I/O 错误照常抛出。"""
    if not jobs:
        return []
    if native.available():
        res = native.run(
            native.OP_VERIFY,
            [
                native.Job(src=j.src, key=j.file_key, uuid=j.file_uuid, expected_size=j.expected_size)
                for j in jobs
            ],
            progress=progress,
            control=control,
            stop_on_error=False,
        )
        out: list[tuple[int, bytes] | VaultError] = []
        for r in res:
            if r.error is None:
                out.append((r.size, r.sha256))
            elif isinstance(r.error, VaultError) and not isinstance(r.error, Cancelled):
                out.append(r.error)
            else:
                raise r.error
        return out
    pp = _PyProgress(progress)
    out = []
    for j in jobs:
        _poll(control)
        try:
            out.append(_py_verify_blob(j.src, j.file_key, j.file_uuid, j.expected_size, pp.add))
        except VaultError as exc:
            out.append(exc)
        pp.file_done()
    return out


def sha256_many(
    paths: list[str | os.PathLike], progress: BatchProgressFn | None = None, control=None
) -> list[bytes]:
    """并行计算一批文件的 sha256。"""
    if not paths:
        return []
    if native.available():
        res = native.run(
            native.OP_HASH, [native.Job(src=p) for p in paths], progress=progress, control=control
        )
        return [r.sha256 for r in res]
    pp = _PyProgress(progress)
    out = []
    for p in paths:
        _poll(control)
        out.append(_py_sha256_file(p, pp.add))
        pp.file_done()
    return out


FINGERPRINT_LEN = 40  # 大小(8) + 设备号(8) + 文件编号(8) + 修改时间(8) + 状态改变时间(8)


def stat_many(paths: list[str | os.PathLike]) -> list[bytes | None]:
    """并行取一批文件的指纹（文件不在 / 取不到时为 None）。

    指纹里有状态改变时间（macOS/Linux 的 ctime、Windows 的 ChangeTime）：文件内容或属性一改它就变，
    普通程序改不回去（utime 只能改修改时间）。两次指纹完全一样 ⇒ 文件没被动过。"""
    if not paths:
        return []
    if native.available():
        res = native.run(native.OP_STAT, [native.Job(src=p) for p in paths], stop_on_error=False)
        return [
            None if r.error is not None else struct.pack("<Q", r.size) + r.sha256 for r in res
        ]
    if os.name == "nt":
        return [None] * len(paths)  # os.stat 拿不到 ChangeTime：不提供指纹，调用方会走完整路径
    out: list[bytes | None] = []
    for p in paths:
        try:
            st = os.lstat(long_path(p))
        except OSError:
            out.append(None)
            continue
        out.append(struct.pack("<QQQQQ", st.st_size, st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns))
    return out


def _single_progress(progress: ProgressFn | None) -> BatchProgressFn | None:
    if progress is None:
        return None
    last = [0]

    def cb(done_bytes: int, _files: int) -> None:
        delta = done_bytes - last[0]
        if delta > 0:
            last[0] = done_bytes
            progress(delta)

    return cb


def encrypt_file(
    src: str | os.PathLike,
    dst: str | os.PathLike,
    file_key: bytes,
    file_uuid: bytes,
    chunk_size: int = C.DEFAULT_CHUNK_SIZE,
    progress: ProgressFn | None = None,
) -> tuple[int, bytes]:
    """把 src 加密写到 dst（调用方负责 tmp+rename）。返回 (明文大小, 明文 sha256)。"""
    job = EncryptJob(src, dst, file_key, file_uuid, chunk_size)
    return encrypt_many([job], _single_progress(progress))[0]


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
    job = DecryptJob(src, dst, file_key, file_uuid, expected_size, expected_sha256)
    return decrypt_many([job], _single_progress(progress))[0][1]


def verify_blob(
    src: str | os.PathLike,
    file_key: bytes,
    file_uuid: bytes,
    expected_size: int | None = None,
    progress: ProgressFn | None = None,
) -> tuple[int, bytes]:
    """只解密不落盘，用于校验/恢复。返回 (明文大小, 明文 sha256)。"""
    r = verify_many([DecryptJob(src, None, file_key, file_uuid, expected_size)], _single_progress(progress))[0]
    if isinstance(r, VaultError):
        raise r
    return r


def sha256_file(path: str | os.PathLike) -> bytes:
    return sha256_many([path])[0]
