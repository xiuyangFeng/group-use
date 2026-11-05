"""上锁后防移动 / 防删除。

保险箱上锁后，文件夹里只剩说明文件和 .vault（密文 + 元数据）。这时候有人把文件夹拖走、
删掉，或者误删 .vault 里的东西，数据就找不回来了。所以上锁完成后给它们加系统级保护：

macOS
    1. 每个条目加「锁定」标记（uchg，访达「显示简介」里的「已锁定」）：移动、改名、删除、
       改写都会返回 Operation not permitted；
    2. 目录再加 ACL「everyone deny delete,delete_child」：就算有人在访达里点了「仍要移到
       废纸篓」（访达会先去掉锁定标记），还是要输管理员密码才删得掉。
Windows
    根目录加一条可继承的拒绝规则：Everyone 不能「删除」(DE) / 「删除子文件夹及文件」(DC)。
    资源管理器里移动、删除会提示「需要权限」；继承到 .vault 里每个文件，不会删一半。

本程序自己做解锁 / 改密码 / 校验等操作时，先撤掉保护（文件的主人有权改这些属性），
操作结束如果还是「已上锁」再加回去，见 core/vault.py 的 @locked。

尽力而为：exFAT U 盘、网络盘等不支持这些属性的文件系统上加不上，上锁本身照常完成，
status() 里 protected=False。
设置 FDVAULT_NO_PROTECT=1 可关闭（自检脚本在系统临时目录里造保险箱时用，免得留下删不掉的临时文件）。
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

from . import constants as C

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"

# macOS：目录上加的 ACL 条目（撤销时用同一个字符串精确删除，不碰用户自己的 ACL）
MAC_ACL = "everyone deny delete,delete_child"
# Windows：Everyone 的 SID，用 SID 避免中文系统里组名不是 "Everyone"
WIN_EVERYONE = "*S-1-1-0"
WIN_DENY = f"{WIN_EVERYONE}:(OI)(CI)(DE,DC)"

UF_IMMUTABLE = getattr(stat, "UF_IMMUTABLE", 0x2)


def enabled() -> bool:
    if os.environ.get("FDVAULT_NO_PROTECT") == "1":
        return False
    return IS_MAC or IS_WINDOWS


# ---------------------------------------------------------------- 底层

def _run(args: list[str]) -> bool:
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "check": False}
    if IS_WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(args, **kwargs).returncode == 0
    except OSError:
        return False


def _flags(p: Path) -> int | None:
    try:
        return os.lstat(p).st_flags  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return None


def _set_immutable(p: Path, on: bool) -> bool:
    flags = _flags(p)
    if flags is None:
        return False
    new = (flags | UF_IMMUTABLE) if on else (flags & ~UF_IMMUTABLE)
    if new == flags:
        return True
    try:
        os.chflags(p, new, follow_symlinks=False)  # type: ignore[attr-defined]
        return True
    except (OSError, NotImplementedError):
        return False


def _is_immutable(p: Path) -> bool:
    flags = _flags(p)
    return bool(flags is not None and flags & UF_IMMUTABLE)


def _vault_entries(root: Path) -> tuple[list[Path], list[Path]]:
    """要保护的范围：根目录本身、根目录下的说明文件、.vault 整棵树。返回 (目录, 文件)。

    只碰这些：上锁后文件夹里本来也只剩它们；万一有用户自己的文件（符号链接等没加密的），
    不去改它们的属性。"""
    vault_dir = root / C.VAULT_DIRNAME
    dirs = [root]
    files: list[Path] = []
    marker = root / C.MARKER_NAME
    if marker.is_file() and not marker.is_symlink():
        files.append(marker)
    if vault_dir.is_dir() and not vault_dir.is_symlink():
        for dirpath, dirnames, filenames in os.walk(vault_dir, followlinks=False):
            dirs.append(Path(dirpath))
            files.extend(Path(dirpath) / f for f in filenames)
    return dirs, files


# ---------------------------------------------------------------- 对外

def is_protected(root: str | os.PathLike) -> bool:
    root = Path(root)
    if IS_MAC:
        return _is_immutable(root) and _is_immutable(root / C.VAULT_DIRNAME)
    if IS_WINDOWS:
        try:
            out = subprocess.run(
                ["icacls", str(root)],
                capture_output=True,
                text=True,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
        except OSError:
            return False
        return any("(DENY)" in line and ("S-1-1-0" in line or "Everyone" in line) for line in out.splitlines())
    return False


def apply(root: str | os.PathLike) -> bool:
    """给已上锁的保险箱加保护。返回是否加上了（不支持时 False，不抛异常）。"""
    if not enabled():
        return False
    root = Path(root)
    if IS_MAC:
        return _apply_mac(root)
    if IS_WINDOWS:
        return _run(["icacls", str(root), "/deny", WIN_DENY, "/C", "/Q"])
    return False


def release(root: str | os.PathLike) -> None:
    """撤掉保护（本程序要改保险箱之前调用）。没加过保护时什么都不做。

    不看 enabled()：关掉开关之前加上的保护也要能撤掉。"""
    root = Path(root)
    if IS_MAC:
        _release_mac(root)
    elif IS_WINDOWS:
        _run(["icacls", str(root), "/remove:d", WIN_EVERYONE, "/C", "/Q"])


def _apply_mac(root: Path) -> bool:
    lock_file = root / C.VAULT_DIRNAME / "instance.lock"
    if not lock_file.exists():
        # .vault 锁定后不能再新建文件；跨进程互斥锁要用它，先建好（它本身不加锁定标记）
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        except OSError:
            pass
    dirs, files = _vault_entries(root)
    # 先加 ACL（加了锁定标记以后就改不了 ACL 了），再从里到外加锁定标记
    for d in dirs:
        if not _has_mac_acl(d):
            _run(["chmod", "+a", MAC_ACL, str(d)])
    ok = True
    for f in files:
        if f == lock_file:
            continue  # 解锁时要先打开它拿互斥锁，必须保持可写
        ok = _set_immutable(f, True) and ok
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        ok = _set_immutable(d, True) and ok
    return ok and _is_immutable(root)


def _release_mac(root: Path) -> None:
    if not root.exists():
        return
    dirs, files = _vault_entries(root)
    # 先从外到里去掉锁定标记，再撤 ACL
    for d in sorted(dirs, key=lambda p: len(p.parts)):
        _set_immutable(d, False)
    for f in files:
        _set_immutable(f, False)
    for d in dirs:
        if _has_mac_acl(d):
            _run(["chmod", "-a", MAC_ACL, str(d)])


def _has_mac_acl(p: Path) -> bool:
    try:
        out = subprocess.run(["ls", "-lde", str(p)], capture_output=True, text=True, check=False).stdout
    except OSError:
        return False
    return "everyone deny delete" in out


def release_tree(path: str | os.PathLike) -> None:
    """把 path 下面所有保险箱的保护都撤掉（测试 / 自检清理临时目录用）。"""
    path = Path(path)
    if not path.exists():
        return
    if IS_WINDOWS:
        _run(["icacls", str(path), "/remove:d", WIN_EVERYONE, "/T", "/C", "/Q"])
        return
    if not IS_MAC:
        return
    roots: list[Path] = []
    for dirpath, dirnames, _ in os.walk(path, followlinks=False):
        if C.VAULT_DIRNAME in dirnames:
            roots.append(Path(dirpath))
    for r in roots:
        _release_mac(r)


def probe(root: str | os.PathLike, elsewhere: str | os.PathLike) -> list[str]:
    """自检用：试着移动 / 改名 / 删除，返回「本该被拦住却成功了」的操作（空列表 = 保护有效）。

    elsewhere 是同一个磁盘上的另一个目录（试移动用）。"""
    import shutil

    root = Path(root)
    attempts = {
        "移动文件夹": lambda: os.rename(root, Path(elsewhere) / (root.name + "-moved")),
        "改名文件夹": lambda: os.rename(root, root.with_name(root.name + "-renamed")),
        "删除说明文件": lambda: os.unlink(root / C.MARKER_NAME),
        "删除 header": lambda: os.unlink(root / C.VAULT_DIRNAME / C.HEADER_NAME),
        "删除整个 .vault": lambda: shutil.rmtree(root / C.VAULT_DIRNAME),
    }
    leaked = []
    for name, fn in attempts.items():
        try:
            fn()
        except OSError:
            continue
        leaked.append(name)
    return leaked
