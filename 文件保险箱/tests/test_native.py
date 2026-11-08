"""C++ 加解密引擎：和 Python 参考实现（cryptography / hashlib）逐字节一致、篡改检测、错误映射、取消 / 暂停。"""

from __future__ import annotations

import hashlib
import os
import random
import threading
import time

import pytest
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from core import native, transform
from core.control import OperationControl
from core.errors import Cancelled, IntegrityError
from core.transform import DecryptJob, EncryptJob

pytestmark = pytest.mark.skipif(not native.available(), reason="C++ 引擎没编译（python scripts/build_native.py）")

CHUNK = 64 * 1024
KEY = bytes(range(32))
UUID = bytes(range(100, 116))


def _write(path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _rand(n: int, seed: int = 1) -> bytes:
    return random.Random(seed).randbytes(n)


# ---------------------------------------------------------------- 引擎本身

def test_selftest_passes_without_disabling_simd():
    info = native.info()
    assert info["engine"] == "C++"
    assert info["selftest"] == "ok", info  # 有 SIMD 被停用会显示 DISABLED
    assert info["threads"] >= 1


@pytest.mark.parametrize("portable", [False, True])
def test_aead_matches_cryptography(portable):
    native.set_force_portable(portable)
    try:
        rng = random.Random(7)
        for n in [0, 1, 15, 16, 17, 63, 64, 65, 255, 256, 257, 511, 512, 513, 1000, 16384, 16385, 100_003]:
            key, nonce = rng.randbytes(32), rng.randbytes(12)
            aad = rng.randbytes(rng.choice([0, 1, 13, 36]))
            pt = rng.randbytes(n)
            ref = ChaCha20Poly1305(key).encrypt(nonce, pt, aad or None)
            assert native.aead_seal(key, nonce, pt, aad) == ref, n
            assert native.aead_open(key, nonce, ref, aad) == pt
            bad = bytearray(ref)
            bad[rng.randrange(len(bad))] ^= 0x01
            assert native.aead_open(key, nonce, bytes(bad), aad) is None
    finally:
        native.set_force_portable(False)


@pytest.mark.parametrize("portable", [False, True])
def test_sha256_matches_hashlib(portable):
    native.set_force_portable(portable)
    try:
        rng = random.Random(3)
        for n in [0, 1, 55, 56, 63, 64, 65, 119, 120, 1000, 65536, 1_000_003]:
            data = rng.randbytes(n)
            assert native.sha256(data) == hashlib.sha256(data).digest(), n
    finally:
        native.set_force_portable(False)


# ---------------------------------------------------------------- 文件格式：和 Python 版逐字节一致

SIZES = [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK + 5, 70 * CHUNK + 123]  # 最后一个超过 64 块的环形窗口


@pytest.mark.parametrize("size", SIZES)
def test_ciphertext_identical_to_python(tmp_path, size):
    data = _rand(size, seed=size)
    src = _write(tmp_path / "plain.bin", data)
    size_n, sha_n = transform.encrypt_file(src, tmp_path / "n.blob", KEY, UUID, chunk_size=CHUNK)
    size_p, sha_p = transform._py_encrypt_file(src, tmp_path / "p.blob", KEY, UUID, chunk_size=CHUNK)
    assert (size_n, sha_n) == (size_p, sha_p) == (size, hashlib.sha256(data).digest())
    assert (tmp_path / "n.blob").read_bytes() == (tmp_path / "p.blob").read_bytes()

    # 交叉解密：Python 写的密文用 C++ 解，C++ 写的用 Python 解
    d1 = transform.decrypt_file(tmp_path / "p.blob", tmp_path / "out1", KEY, UUID, size, sha_p)
    d2 = transform._py_decrypt_file(tmp_path / "n.blob", tmp_path / "out2", KEY, UUID, size, sha_n)
    assert d1 == d2 == sha_p
    assert (tmp_path / "out1").read_bytes() == data == (tmp_path / "out2").read_bytes()
    assert transform.verify_blob(tmp_path / "n.blob", KEY, UUID, size) == (size, sha_p)


def test_simd_and_portable_produce_same_blob(tmp_path):
    src = _write(tmp_path / "plain.bin", _rand(9 * CHUNK + 77))
    transform.encrypt_file(src, tmp_path / "fast.blob", KEY, UUID, chunk_size=CHUNK)
    native.set_force_portable(True)
    try:
        transform.encrypt_file(src, tmp_path / "slow.blob", KEY, UUID, chunk_size=CHUNK)
    finally:
        native.set_force_portable(False)
    assert (tmp_path / "fast.blob").read_bytes() == (tmp_path / "slow.blob").read_bytes()


@pytest.mark.parametrize("threads", [1, 3, 16])
def test_batch_many_files_roundtrip(tmp_path, threads):
    rng = random.Random(threads)
    plain_dir, blob_dir, out_dir = tmp_path / "p", tmp_path / "b", tmp_path / "o"
    blob_dir.mkdir()
    out_dir.mkdir()
    files = []
    for i in range(120):
        size = rng.choice([0, 1, 100, CHUNK, CHUNK + 1, rng.randrange(1, 6 * CHUNK)])
        key, uid = rng.randbytes(32), rng.randbytes(16)
        data = rng.randbytes(size)
        files.append((_write(plain_dir / f"子目录{i % 7}" / f"文件 {i}.dat", data), key, uid, data))

    enc = [EncryptJob(p, blob_dir / f"{i}.blob", k, u, CHUNK, size_hint=len(d)) for i, (p, k, u, d) in enumerate(files)]
    res = native.run(
        native.OP_ENCRYPT,
        [native.Job(j.src, j.dst, j.file_key, j.file_uuid, j.chunk_size, j.size_hint) for j in enc],
        threads=threads,
    )
    for (p, k, u, d), r, j in zip(files, res, enc):
        assert r.error is None and r.size == len(d) and r.sha256 == hashlib.sha256(d).digest()
        # 每个文件都能被 Python 参考实现解开
        assert transform._py_verify_blob(j.dst, k, u, len(d)) == (len(d), r.sha256)

    dec = [DecryptJob(j.dst, out_dir / f"{i}.out", j.file_key, j.file_uuid, len(files[i][3]), res[i].sha256) for i, j in enumerate(enc)]
    out = transform.decrypt_many(dec)
    for (p, k, u, d), j, (size, sha) in zip(files, dec, out):
        assert size == len(d)
        assert open(j.dst, "rb").read() == d

    sums = transform.sha256_many([f[0] for f in files])
    assert sums == [hashlib.sha256(f[3]).digest() for f in files]


def test_progress_reports_bytes_and_files(tmp_path):
    srcs = [_write(tmp_path / f"{i}.bin", _rand(5 * CHUNK + i, seed=i)) for i in range(6)]
    seen: list[tuple[int, int]] = []
    jobs = [EncryptJob(s, tmp_path / f"{i}.blob", KEY, UUID, CHUNK) for i, s in enumerate(srcs)]
    transform.encrypt_many(jobs, progress=lambda b, f: seen.append((b, f)))
    total = sum(os.path.getsize(s) for s in srcs)
    assert seen, "至少结束时会回调一次"
    assert seen[-1] == (total, len(srcs))
    assert all(b2 >= b1 and f2 >= f1 for (b1, f1), (b2, f2) in zip(seen, seen[1:]))


# ---------------------------------------------------------------- 篡改 / 损坏

def _make_blob(tmp_path, size=6 * CHUNK + 10):
    data = _rand(size, seed=99)
    src = _write(tmp_path / "plain.bin", data)
    blob = tmp_path / "x.blob"
    _size, sha = transform.encrypt_file(src, blob, KEY, UUID, chunk_size=CHUNK)
    return blob, data, sha


def _expect(blob, message, *, uuid=UUID, size=None):
    with pytest.raises(IntegrityError, match=message):
        transform.verify_blob(blob, KEY, uuid, size)


def test_tamper_flip_middle_chunk(tmp_path):
    blob, _d, _s = _make_blob(tmp_path)
    raw = bytearray(blob.read_bytes())
    raw[transform.HEADER.size + 3 * (CHUNK + 16) + 5] ^= 0x80
    blob.write_bytes(raw)
    _expect(blob, "密文校验失败")


def test_tamper_swap_chunks(tmp_path):
    blob, _d, _s = _make_blob(tmp_path)
    raw = blob.read_bytes()
    h, n = transform.HEADER.size, CHUNK + 16
    c1, c2 = raw[h + n : h + 2 * n], raw[h + 2 * n : h + 3 * n]
    blob.write_bytes(raw[: h + n] + c2 + c1 + raw[h + 3 * n :])
    _expect(blob, "密文校验失败")


def test_tamper_truncate_trailing_header(tmp_path):
    blob, _d, _s = _make_blob(tmp_path)
    raw = blob.read_bytes()
    blob.write_bytes(raw[:-1])
    _expect(blob, "密文被截断")
    blob.write_bytes(raw + b"x")
    _expect(blob, "密文尾部有多余数据")
    blob.write_bytes(raw[:10])
    _expect(blob, "密文文件头不完整")
    blob.write_bytes(b"XXXX" + raw[4:])
    _expect(blob, "密文格式不对")
    blob.write_bytes(raw)
    _expect(blob, "密文大小与记录不一致", size=123)
    _expect(blob, "密文校验失败", uuid=bytes(16))  # 换了文件 uuid（跨文件拼接）


def test_decrypt_wrong_hash_removes_output(tmp_path):
    blob, _d, _sha = _make_blob(tmp_path)
    out = tmp_path / "out.bin"
    with pytest.raises(IntegrityError, match="解密后哈希与记录不一致"):
        transform.decrypt_file(blob, out, KEY, UUID, None, b"\x00" * 32)
    assert not out.exists(), "校验失败时不能留下半成品明文"


def test_verify_many_reports_per_file(tmp_path):
    good, _d, sha = _make_blob(tmp_path)
    bad = tmp_path / "bad.blob"
    raw = bytearray(good.read_bytes())
    raw[-3] ^= 1
    bad.write_bytes(raw)
    res = transform.verify_many([DecryptJob(good, None, KEY, UUID), DecryptJob(bad, None, KEY, UUID)])
    assert res[0][1] == sha
    assert isinstance(res[1], IntegrityError)


# ---------------------------------------------------------------- 系统错误映射

def test_missing_source_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError) as ei:
        transform.encrypt_file(tmp_path / "没有这个文件.bin", tmp_path / "x.blob", KEY, UUID, CHUNK)
    assert "没有这个文件.bin" in str(ei.value.filename)


def test_missing_destination_dir_reports_destination(tmp_path):
    src = _write(tmp_path / "a.bin", b"hello")
    dst = tmp_path / "不存在的目录" / "x.blob"
    with pytest.raises(FileNotFoundError) as ei:
        transform.encrypt_file(src, dst, KEY, UUID, CHUNK)
    assert ei.value.filename == str(dst)


def test_stop_on_error_raises_first_failure(tmp_path):
    srcs = [_write(tmp_path / f"{i}.bin", _rand(CHUNK * 2, seed=i)) for i in range(20)]
    jobs = [EncryptJob(s, tmp_path / f"{i}.blob", KEY, UUID, CHUNK) for i, s in enumerate(srcs)]
    jobs[7] = EncryptJob(tmp_path / "missing.bin", tmp_path / "7.blob", KEY, UUID, CHUNK)
    with pytest.raises(FileNotFoundError):
        transform.encrypt_many(jobs)


# ---------------------------------------------------------------- 取消 / 暂停

def _big_batch(tmp_path, n=40, size=8 * CHUNK):
    srcs = [_write(tmp_path / "src" / f"{i}.bin", _rand(size, seed=i)) for i in range(n)]
    (tmp_path / "dst").mkdir()
    return [EncryptJob(s, tmp_path / "dst" / f"{i}.blob", KEY, UUID, CHUNK) for i, s in enumerate(srcs)]


def test_cancel_before_start(tmp_path):
    jobs = _big_batch(tmp_path, n=3)
    control = OperationControl()
    control.cancel()
    with pytest.raises(Cancelled):
        transform.encrypt_many(jobs, control=control)


def test_cancel_midway_leaves_no_partial_outputs(tmp_path):
    jobs = _big_batch(tmp_path, n=200, size=16 * CHUNK)
    control = OperationControl()

    def on_progress(b, f):
        if b > 0:
            control.cancel()

    with pytest.raises(Cancelled):
        transform.encrypt_many(jobs, progress=on_progress, control=control)
    # 没做完的文件都被引擎删掉；做完的是完整密文（调用方的 finally 会清理 tmp）
    for j in jobs:
        if os.path.exists(j.dst):
            size = os.path.getsize(j.dst)
            assert size == transform.HEADER.size + 16 * CHUNK + 16 * 16


def test_pause_then_resume(tmp_path):
    jobs = _big_batch(tmp_path, n=30)
    control = OperationControl()
    control.pause()
    done = threading.Event()
    result: dict = {}

    def worker():
        try:
            result["r"] = transform.encrypt_many(jobs, control=control)
        except Exception as exc:  # noqa: BLE001
            result["e"] = exc
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    time.sleep(0.5)
    assert not done.is_set(), "暂停时不应该完成"
    control.resume()
    assert done.wait(60)
    assert "e" not in result and len(result["r"]) == 30


def test_progress_callback_exception_propagates(tmp_path):
    jobs = _big_batch(tmp_path, n=50)

    def boom(_b, _f):
        raise RuntimeError("回调出错")

    with pytest.raises(RuntimeError, match="回调出错"):
        transform.encrypt_many(jobs, progress=boom)


# ---------------------------------------------------------------- 强制 Python 兜底

def test_python_fallback_roundtrip(tmp_path, monkeypatch):
    blob, data, sha = _make_blob(tmp_path)
    monkeypatch.setenv("FDVAULT_ENGINE", "python")
    assert transform.engine_name() == "Python"
    out = tmp_path / "py.out"
    assert transform.decrypt_file(blob, out, KEY, UUID, len(data), sha) == sha
    assert out.read_bytes() == data
