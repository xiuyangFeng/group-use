"""C++ 加解密引擎（native/ 目录）的 ctypes 封装。

引擎一次处理一批文件：多文件并行、大文件内部按块并行，用满所有 CPU 核和磁盘队列。
密文格式和 core/transform.py 里的 Python 参考实现完全一致，两者可以互相解密。

找库的顺序：
  1. 环境变量 FDVAULT_NATIVE_LIB（完整路径）
  2. 打包版：sys._MEIPASS/core/_native/
  3. 源码版：core/_native/（scripts/build_native.py 编译后放在这里）

FDVAULT_ENGINE=python 强制用 Python 实现（测试交叉比对用）；
FDVAULT_THREADS=N 指定线程数（默认 = 逻辑核数）。
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .errors import Cancelled, IntegrityError
from .fsutil import IS_WINDOWS, long_path

OP_ENCRYPT = 1
OP_DECRYPT = 2
OP_VERIFY = 3
OP_HASH = 4
OP_STAT = 5

OK = 0
E_IO = 1
E_CANCELLED = 2
E_SKIPPED = 3
E_NOMEM = 4
E_SRC_SHRANK = 19

ABI_VERSION = 2

# 和 core/transform.py 的报错文字保持一致（GUI / 测试按这些文字提示用户）
_INTEGRITY_MESSAGES = {
    10: "密文文件头不完整",
    11: "密文格式不对（可能文件损坏）",
    12: "密文头部的块大小不合法",
    13: "密文大小与记录不一致",
    14: "密文块数不对",
    15: "密文被截断",
    16: "密文校验失败（文件损坏或被篡改）",
    17: "密文尾部有多余数据",
    18: "解密后哈希与记录不一致（数据可能已损坏）",
    20: "加密结果大小不对",
}

# fdv_where：出错步骤对应的文件（源 / 目标）
_WHERE_DST = {2, 4, 5}

ProgressFn = Callable[[int, int], None]  # (已处理字节, 已完成文件数)


class _Job(ctypes.Structure):
    _fields_ = [
        ("src", ctypes.c_char_p),
        ("dst", ctypes.c_char_p),
        ("key", ctypes.c_uint8 * 32),
        ("uuid", ctypes.c_uint8 * 16),
        ("expected_sha", ctypes.c_uint8 * 32),
        ("expected_size", ctypes.c_int64),
        ("chunk_size", ctypes.c_uint32),
        ("has_expected_sha", ctypes.c_int32),
        ("out_size", ctypes.c_int64),
        ("out_sha", ctypes.c_uint8 * 32),
        ("status", ctypes.c_int32),
        ("sys_error", ctypes.c_int32),
        ("where", ctypes.c_int32),
        ("reserved", ctypes.c_int32),
    ]


_PROGRESS = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_void_p)


@dataclass
class Job:
    src: str | os.PathLike
    dst: str | os.PathLike | None = None
    key: bytes = b""
    uuid: bytes = b""
    chunk_size: int = 0
    expected_size: int | None = None  # 解密/校验：要求的明文大小；加密：大小提示（大文件先做）
    expected_sha256: bytes = b""


@dataclass
class Result:
    size: int
    sha256: bytes
    error: BaseException | None  # None 表示成功


def lib_filename() -> str:
    if IS_WINDOWS:
        return "fdvnative.dll"
    if sys.platform == "darwin":
        return "libfdvnative.dylib"
    return "libfdvnative.so"


def _candidates() -> list[Path]:
    out: list[Path] = []
    env = os.environ.get("FDVAULT_NATIVE_LIB")
    if env:
        out.append(Path(env))
    name = lib_filename()
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(Path(meipass) / "core" / "_native" / name)
        out.append(Path(meipass) / name)
    out.append(Path(__file__).resolve().parent / "_native" / name)
    return out


_lock = threading.Lock()
_lib: ctypes.CDLL | None = None
_lib_path: Path | None = None
_load_error: str | None = None
_selftest: str = ""
_loaded = False


def _bind(lib: ctypes.CDLL) -> None:
    lib.fdv_run.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(_Job),
        ctypes.c_size_t,
        ctypes.c_int,
        ctypes.c_int,
        _PROGRESS,
        ctypes.c_void_p,
    ]
    lib.fdv_run.restype = ctypes.c_int
    lib.fdv_abi_version.restype = ctypes.c_int
    lib.fdv_job_size.restype = ctypes.c_size_t
    lib.fdv_default_threads.restype = ctypes.c_int
    lib.fdv_version.restype = ctypes.c_char_p
    lib.fdv_impl_info.restype = ctypes.c_char_p
    lib.fdv_selftest.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
    lib.fdv_selftest.restype = ctypes.c_int
    lib.fdv_aead_seal.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
    ]
    lib.fdv_aead_seal.restype = None
    lib.fdv_aead_open.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p,
    ]
    lib.fdv_aead_open.restype = ctypes.c_int
    lib.fdv_sha256.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p]
    lib.fdv_sha256.restype = None
    lib.fdv_set_force_portable.argtypes = [ctypes.c_int]
    lib.fdv_set_force_portable.restype = None


def _load() -> ctypes.CDLL | None:
    global _lib, _lib_path, _load_error, _selftest, _loaded
    with _lock:
        if _loaded:
            return _lib
        _loaded = True
        errors: list[str] = []
        for path in _candidates():
            if not path.is_file():
                continue
            try:
                lib = ctypes.CDLL(str(path))
                _bind(lib)
                if lib.fdv_abi_version() != ABI_VERSION:
                    raise OSError(f"接口版本不对：{lib.fdv_abi_version()}，需要 {ABI_VERSION}")
                if lib.fdv_job_size() != ctypes.sizeof(_Job):
                    raise OSError("fdv_job 结构体大小与 Python 端不一致")
                buf = ctypes.create_string_buffer(512)
                rc = lib.fdv_selftest(buf, len(buf))
                _selftest = buf.value.decode("utf-8", "replace")
                if rc != 0:
                    raise OSError(f"C++ 引擎自检没通过：{_selftest}")
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                continue
            _lib, _lib_path = lib, path
            _load_error = None
            return lib
        _load_error = "；".join(errors) if errors else "没找到 C++ 引擎库（" + lib_filename() + "）"
        return None


def available() -> bool:
    if os.environ.get("FDVAULT_ENGINE", "").lower() == "python":
        return False
    return _load() is not None


def library() -> ctypes.CDLL:
    lib = _load()
    if lib is None:
        raise RuntimeError(_load_error or "C++ 引擎不可用")
    return lib


def default_threads() -> int:
    raw = os.environ.get("FDVAULT_THREADS", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    lib = _load()
    return int(lib.fdv_default_threads()) if lib is not None else (os.cpu_count() or 1)


def info() -> dict:
    """给诊断信息 / 界面显示用。"""
    if available():
        lib = library()
        return {
            "engine": "C++",
            "version": lib.fdv_version().decode(),
            "impl": lib.fdv_impl_info().decode(),
            "threads": default_threads(),
            "selftest": _selftest,
            "path": str(_lib_path),
        }
    forced = os.environ.get("FDVAULT_ENGINE", "").lower() == "python"
    _load()
    return {
        "engine": "Python",
        "reason": "FDVAULT_ENGINE=python" if forced else (_load_error or ""),
    }


def _encode_path(p: str | os.PathLike) -> bytes:
    s = long_path(p)
    if IS_WINDOWS:
        return s.encode("utf-8", "surrogatepass")
    return os.fsencode(s)


def _os_error(code: int, filename: str) -> OSError:
    if IS_WINDOWS:
        msg = ctypes.FormatError(code).strip() if hasattr(ctypes, "FormatError") else f"Windows 错误 {code}"
        return OSError(0, msg, filename, code)
    return OSError(code, os.strerror(code), filename)


def _error_for(job: Job, raw: _Job) -> BaseException | None:
    st = raw.status
    if st == OK:
        return None
    if st in _INTEGRITY_MESSAGES:
        return IntegrityError(_INTEGRITY_MESSAGES[st])
    if st == E_SRC_SHRANK:
        return IntegrityError(f"读取 {os.fspath(job.src)} 时意外结束")
    if st == E_IO:
        target = job.dst if raw.where in _WHERE_DST and job.dst is not None else job.src
        return _os_error(raw.sys_error, os.fspath(target))
    if st == E_CANCELLED:
        return Cancelled("操作已取消")
    if st == E_SKIPPED:
        return _Skipped()
    if st == E_NOMEM:
        return MemoryError("内存不够")
    return IntegrityError(f"C++ 引擎返回未知状态 {st}")


class _Skipped(Exception):
    """前面的文件出错后没有处理的文件（不会抛给调用方）。"""


def run(
    op: int,
    jobs: list[Job],
    *,
    progress: ProgressFn | None = None,
    control=None,
    stop_on_error: bool = True,
    threads: int | None = None,
) -> list[Result]:
    """在 C++ 引擎里跑一批任务。

    - control（core.control.OperationControl）：暂停 / 取消会传给引擎；取消时抛 Cancelled。
    - stop_on_error=True：第一个出错的文件让整批停下，并把那个错误抛出来；
      False：每个文件的结果（含错误）都放在返回值里，调用方自己处理。
    """
    lib = library()
    if not jobs:
        return []
    n = len(jobs)
    arr = (_Job * n)()
    keep: list[bytes] = []  # 保证路径字节串在调用期间不被回收
    for i, j in enumerate(jobs):
        raw = arr[i]
        src = _encode_path(j.src)
        keep.append(src)
        raw.src = src
        if j.dst is not None:
            dst = _encode_path(j.dst)
            keep.append(dst)
            raw.dst = dst
        if len(j.key) != 32 or len(j.uuid) != 16:
            if op not in (OP_HASH, OP_STAT):
                raise ValueError("key 必须 32 字节、uuid 必须 16 字节")
        if j.key:
            ctypes.memmove(raw.key, j.key, 32)
        if j.uuid:
            ctypes.memmove(raw.uuid, j.uuid, 16)
        if j.expected_sha256:
            ctypes.memmove(raw.expected_sha, j.expected_sha256, 32)
            raw.has_expected_sha = 1
        raw.expected_size = -1 if j.expected_size is None else int(j.expected_size)
        raw.chunk_size = int(j.chunk_size)

    callback_error: list[BaseException] = []

    def _cb(done_bytes: int, done_files: int, _user) -> int:
        try:
            if control is not None:
                if control.cancelled:
                    return 1
                if control.paused:
                    return 2
            if progress is not None:
                progress(int(done_bytes), int(done_files))
        except BaseException as exc:  # noqa: BLE001 —— 不能让异常穿过 C 代码
            callback_error.append(exc)
            return 1
        return 0

    cb = _PROGRESS(_cb)
    rc = lib.fdv_run(op, arr, n, threads or default_threads(), 1 if stop_on_error else 0, cb, None)
    if rc == -1:
        raise ValueError("C++ 引擎参数错误")
    if rc == -2:
        raise RuntimeError(f"C++ 引擎自检没通过，拒绝工作：{_selftest}")
    if callback_error:
        raise callback_error[0]
    if rc == 2:
        raise Cancelled("操作已取消")

    results: list[Result] = []
    first_error: BaseException | None = None
    for j, raw in zip(jobs, arr):
        err = _error_for(j, raw)
        if err is not None and not isinstance(err, _Skipped) and first_error is None:
            first_error = err
        results.append(Result(int(raw.out_size), bytes(raw.out_sha), err))
    if stop_on_error and first_error is not None:
        raise first_error
    return results


# ---------------------------------------------------------------- 单块原语（测试用）

def aead_seal(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    lib = library()
    buf = ctypes.create_string_buffer(plaintext, len(plaintext) + 16)
    tag_ptr = ctypes.addressof(buf) + len(plaintext)
    lib.fdv_aead_seal(key, nonce, aad, len(aad), buf, len(plaintext), tag_ptr)
    return buf.raw[: len(plaintext) + 16]


def aead_open(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes = b"") -> bytes | None:
    lib = library()
    if len(ciphertext) < 16:
        return None
    n = len(ciphertext) - 16
    buf = ctypes.create_string_buffer(ciphertext[:n], max(n, 1))
    ok = lib.fdv_aead_open(key, nonce, aad, len(aad), buf, n, ciphertext[n:])
    return buf.raw[:n] if ok else None


def sha256(data: bytes) -> bytes:
    lib = library()
    out = ctypes.create_string_buffer(32)
    lib.fdv_sha256(data, len(data), out)
    return out.raw


def set_force_portable(on: bool) -> None:
    library().fdv_set_force_portable(1 if on else 0)
