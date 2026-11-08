"""文件系统工具：原子写、fsync、扫描目录树、跨平台路径处理。"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = os.name == "nt"

def _busy_windows(paths: list[Path]) -> list[Path]:
    """Windows：以读写方式打开失败（共享冲突）但文件本身可写 → 认为被占用。

    文件多时并行检查（每次打开都要过一遍杀毒软件，串行 2 万个文件要好几秒）。"""

    def one(p: Path) -> bool:
        try:
            fd = os.open(str(p), os.O_RDWR)
        except PermissionError:
            return os.access(str(p), os.W_OK)
        except OSError:
            return False
        os.close(fd)
        return False

    if len(paths) < 64:
        flags = [one(p) for p in paths]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(32, (os.cpu_count() or 4) * 2)) as ex:
            flags = list(ex.map(one, paths, chunksize=64))
    return [p for p, busy in zip(paths, flags) if busy]


def _busy_macos(paths: list[Path]) -> list[Path]:
    """macOS：用 lsof 查询（Excel、Preview 等会持有文件）。

    只跑一次 lsof 列出所有打开的文件，再和要上锁的文件比对：按 200 个一组传路径给 lsof，
    2 万个文件要跑 100 次、十几秒；整表一次只要零点几秒。"""
    import subprocess

    if not paths:
        return []
    try:
        proc = subprocess.run(
            ["lsof", "-n", "-P", "-w", "-F", "n"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode not in (0, 1):
        return []
    # lsof 输出的是解析过符号链接的真实路径（/var → /private/var），
    # 这边也做 realpath 再比，否则 /tmp、外接盘等路径会漏检
    # 每个目录只做一次 realpath（2 万个文件逐个 realpath 要几十万次 lstat）；
    # 文件本身不是符号链接（扫描时已排除），所以「目录真实路径 + 文件名」就是文件的真实路径
    real_dirs: dict[str, str] = {}
    keys: list[str] = []
    for p in paths:
        d, name = os.path.split(str(p))
        rd = real_dirs.get(d)
        if rd is None:
            rd = real_dirs[d] = os.path.realpath(d)
        keys.append(os.path.join(rd, name))
    wanted = set(keys)
    busy_names = {line[1:] for line in proc.stdout.splitlines() if line.startswith("n") and line[1:] in wanted}
    return [p for p, k in zip(paths, keys) if k in busy_names]


def busy_files(paths: list[Path]) -> list[Path]:
    """返回正在被其它程序占用的文件（best effort）。

    - Windows：调用 `_busy_windows`（共享冲突检测）。
    - macOS：调用 `_busy_macos`（lsof）。
    - Linux：开发环境不检查，返回空列表（交付不含 Linux 版）。
    设 FDVAULT_SKIP_BUSY_CHECK=1 可跳过（测试用）。
    """
    if os.environ.get("FDVAULT_SKIP_BUSY_CHECK") == "1":
        return []
    paths = [Path(p) for p in paths]
    if not paths:
        return []
    if IS_WINDOWS:
        return _busy_windows(paths)
    if sys.platform == "darwin":
        return _busy_macos(paths)
    return []
def long_path(p: str | os.PathLike) -> str:
    """Windows 下为超长路径加 \\\\?\\ 前缀；其它平台原样返回。"""
    s = os.fspath(p)
    if not IS_WINDOWS:
        return s
    if s.startswith("\\\\?\\"):
        return s
    ap = os.path.abspath(s)
    if ap.startswith("\\\\"):  # UNC
        return "\\\\?\\UNC\\" + ap[2:]
    if len(ap) >= 240:
        return "\\\\?\\" + ap
    return s


def to_rel(path: str, root: str) -> str:
    """把绝对路径转成相对 root、使用 '/' 分隔的字符串。"""
    rel = os.path.relpath(path, root)
    return rel.replace(os.sep, "/") if os.sep != "/" else rel


def from_rel(rel: str) -> str:
    """把 '/'-分隔的相对路径还原为当前平台路径。"""
    return rel.replace("/", os.sep) if os.sep != "/" else rel


def safe_rel(rel: str) -> bool:
    """拒绝绝对路径和 .. 穿越。"""
    if not rel or rel.startswith("/") or rel.startswith("\\"):
        return False
    parts = rel.replace("\\", "/").split("/")
    return all(p not in ("", ".", "..") for p in parts)


# ---------------------------------------------------------------- fsync

def fsync_dir(path: str | os.PathLike) -> None:
    """把目录项变更（rename/unlink）刷到磁盘。Windows 不支持，静默跳过。"""
    if IS_WINDOWS:
        return
    fd = os.open(long_path(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_path(path: str | os.PathLike) -> None:
    fd = os.open(long_path(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- 原子写

def atomic_write_bytes(path: str | os.PathLike, data: bytes, mode: int = 0o600) -> None:
    p = Path(path)
    tmp = p.with_name(p.name + ".fdv-tmp")
    with open(long_path(tmp), "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(long_path(tmp), long_path(p))
    try:
        os.chmod(long_path(p), mode)
    except OSError:
        pass
    fsync_dir(p.parent)


def atomic_write_json(path: str | os.PathLike, obj: object, mode: int = 0o600) -> None:
    data = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    atomic_write_bytes(path, data, mode)


def read_json(path: str | os.PathLike) -> object:
    with open(long_path(path), "rb") as f:
        return json.loads(f.read().decode("utf-8"))


# ---------------------------------------------------------------- 空间

def free_bytes(path: str | os.PathLike) -> int:
    return shutil.disk_usage(os.fspath(path)).free


# ---------------------------------------------------------------- 权限 / 时间

def set_mode(path: str | os.PathLike, mode: int) -> None:
    if IS_WINDOWS:
        return
    try:
        os.chmod(long_path(path), mode & 0o7777)
    except OSError:
        pass


def set_mtime_ns(path: str | os.PathLike, mtime_ns: int) -> None:
    try:
        os.utime(long_path(path), ns=(mtime_ns, mtime_ns))
    except OSError:
        pass


def file_meta(path: str | os.PathLike) -> tuple[int, int, int]:
    """返回 (size, mtime_ns, mode&0o7777)。"""
    st = os.stat(long_path(path), follow_symlinks=False)
    return st.st_size, st.st_mtime_ns, st.st_mode & 0o7777


def remove_file(path: str | os.PathLike) -> None:
    os.unlink(long_path(path))


# ---------------------------------------------------------------- 扫描

@dataclass
class FsEntry:
    rel: str
    is_dir: bool
    size: int
    mtime_ns: int
    mode: int


def _excluded(rel: str, name: str, exclusions: set[str]) -> bool:
    if rel == "" and name in exclusions:
        return True
    return any(rel.endswith(s) or name.endswith(s) for s in (".fdv-part", ".fdv-tmp"))


def scan_tree(root: str | os.PathLike, exclusions: set[str]) -> tuple[list[FsEntry], int, list[str]]:
    """扫描文件夹，返回 (条目列表, 总字节数, 跳过项)。

    - 跳过 exclusions 中列出的根目录下名字（.vault、说明文件）
    - 跳过临时文件
    - 不跟随符号链接；跳过符号链接和特殊文件，并记录到「跳过项」
    """
    root_s = os.fspath(root)
    entries: list[FsEntry] = []
    total = 0
    skipped: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root_s, topdown=True, followlinks=False):
        dirnames.sort()
        filenames.sort()
        keep_dirs = []
        for d in dirnames:
            p = os.path.join(dirpath, d)
            rel = to_rel(p, root_s)
            if rel.count("/") == 0 and d in exclusions:
                continue
            if _excluded(rel, d, exclusions):
                continue
            if os.path.islink(p):
                skipped.append(rel + "/")
                continue
            st = os.stat(p, follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode):
                skipped.append(rel)
                continue
            entries.append(FsEntry(rel, True, 0, st.st_mtime_ns, st.st_mode & 0o7777))
            keep_dirs.append(d)
        dirnames[:] = keep_dirs

        for f in filenames:
            p = os.path.join(dirpath, f)
            rel = to_rel(p, root_s)
            if rel.count("/") == 0 and f in exclusions:
                continue
            if _excluded(rel, f, exclusions):
                continue
            if os.path.islink(p):
                skipped.append(rel)
                continue
            st = os.stat(p, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                skipped.append(rel)
                continue
            entries.append(FsEntry(rel, False, st.st_size, st.st_mtime_ns, st.st_mode & 0o7777))
            total += st.st_size

    return entries, total, skipped


def sha256_file(path: str | os.PathLike, chunk: int = 1024 * 1024) -> bytes:
    import hashlib

    h = hashlib.sha256()
    with open(long_path(path), "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.digest()


def human_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    x = float(n)
    for u in units:
        if x < 1024 or u == units[-1]:
            return f"{x:.1f} {u}" if u != "B" else f"{int(x)} B"
        x /= 1024
    return f"{n} B"


def ensure_dir(path: str | os.PathLike, mode: int = 0o700) -> None:
    os.makedirs(long_path(path), exist_ok=True)
    try:
        os.chmod(long_path(path), mode)
    except OSError:
        pass


def platform_data_dir() -> Path:
    """审计日志等状态的默认目录（不放进保险箱）。

    FDVAULT_DATA_DIR 可以改到别处：测试和自检用它，免得往真实的审计日志 / 设置里写测试记录。"""
    override = os.environ.get("FDVAULT_DATA_DIR")
    if override:
        return Path(override).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "文件保险箱"
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or str(home)
        return Path(base) / "文件保险箱"
    base = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    return Path(base) / "文件保险箱"
