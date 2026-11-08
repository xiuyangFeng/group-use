"""上锁后防移动 / 防删除。

保险箱上锁后，文件夹里只剩说明文件和 .vault（密文 + 元数据）。这时候有人把文件夹拖走、
删掉，或者误删 .vault 里的东西，数据就找不回来了。所以上锁完成后给它们加系统级保护：

macOS
    1. 每个条目加「锁定」标记（uchg，访达「显示简介」里的「已锁定」）：移动、改名、删除、
       改写都会返回 Operation not permitted；
    2. 目录再加 ACL「everyone deny delete,delete_child」：就算有人在访达里点了「仍要移到
       废纸篓」（访达会先去掉锁定标记），还是要输管理员密码才删得掉。
Windows
    1. 根目录加一条可继承的拒绝规则：Everyone 不能「删除」(DE) / 「删除子文件夹及文件」(DC)，
       继承到 .vault 里每个文件：里面的东西一个都删不掉、不会删一半；
    2. 父目录（如桌面）加一条只作用于它自己的「拒绝删除子项」(DC)。NTFS 的规则是：父目录给了
       「删除子项」权限，子文件夹自己拒绝删除也没用，照样能被移动 / 改名 / 扔进回收站。
       这条只去掉「靠父目录删」这条路：桌面上其它文件本身继承了删除权限，照常能删、能挪。
       父目录下没有别的上锁保险箱时，撤保护会把它一起撤掉；父目录本来就有 Everyone 的拒绝规则、
       或者没权限改（如 D 盘根目录），就不碰父目录，只做第 1 步（这时文件夹整体还能被挪走）。

    3. **占用句柄**（只在图形界面程序运行期间）：对每个已上锁的保险箱根目录持有一个
       不允许「共享删除」的目录句柄。Windows 上移动 / 改名 / 删除文件夹都要以 DELETE 权限打开它，
       有这个句柄在就会被拒绝（「操作无法完成，因为文件夹已在另一程序中打开」）——
       不依赖 NTFS 权限，所以 U 盘（exFAT/FAT32）、磁盘根目录（如 D:\保险箱）、
       管理员点了「继续」也一样拦得住。程序退出后只剩第 1、2 步的权限规则。
       代价：程序开着时，放着已上锁保险箱的 U 盘不能「安全弹出」，要先退出程序。

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
WIN_PARENT_DENY = f"{WIN_EVERYONE}:(DC)"   # 只作用于父目录本身，不继承

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
        return bool(_win_everyone_denies(root))
    return False


def _win_everyone_denies(p: Path) -> list[str]:
    """icacls 输出里 Everyone 的拒绝规则（只看这个对象自己的，不含继承来的 (I)）。"""
    return _win_everyone_denies_or_none(p) or []


def _win_everyone_denies_or_none(p: Path) -> list[str] | None:
    """同上；icacls 没跑成功时返回 None（不知道有没有规则）。"""
    try:
        proc = subprocess.run(
            ["icacls", str(p)],
            capture_output=True,
            text=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout or ""
    rules = []
    for line in out.splitlines():
        if "(DENY)" not in line or "(I)" in line:
            continue
        if "S-1-1-0" in line or "Everyone" in line:
            rules.append(line.split(":", 1)[-1].strip() if ":" in line else line.strip())
    return rules


def _win_is_our_parent_rule(rules: list[str]) -> bool:
    return bool(rules) and all(r.replace(" ", "").endswith("(DENY)(DC)") for r in rules)


def _apply_windows(root: Path) -> bool:
    ok = _run(["icacls", str(root), "/deny", WIN_DENY, "/C", "/Q"])
    parent = root.parent
    if ok and parent != root:
        existing = _win_everyone_denies(parent)
        if not existing:
            _run(["icacls", str(parent), "/deny", WIN_PARENT_DENY, "/Q"])
        # existing 是我们之前加的（同目录下另一个保险箱）就不用再加；是别人的规则就不碰
    return ok


def _win_locked_siblings(parent: Path, exclude: Path) -> bool:
    """父目录下还有没有别的（受保护的）保险箱。"""
    try:
        children = list(parent.iterdir())
    except OSError:
        return False
    for child in children:
        if child == exclude or not (child / C.VAULT_DIRNAME / C.HEADER_NAME).exists():
            continue
        if _win_everyone_denies(child):
            return True
    return False


def _release_windows(root: Path) -> None:
    # 没加过规则就别调 /remove:d：它照样会重写整棵树的继承权限，每个文件的「状态改变时间」
    # 都会变，增量上锁就会把没改过的文件全当成改过的重新加密（文件多时也很慢）。
    # 读不到（None）时宁可照旧撤一次。
    if _win_everyone_denies_or_none(root) != []:
        _run(["icacls", str(root), "/remove:d", WIN_EVERYONE, "/C", "/Q"])
    parent = root.parent
    if parent == root:
        return
    if _win_is_our_parent_rule(_win_everyone_denies(parent)) and not _win_locked_siblings(parent, root):
        _run(["icacls", str(parent), "/remove:d", WIN_EVERYONE, "/Q"])


def apply(root: str | os.PathLike) -> bool:
    """给已上锁的保险箱加保护。返回是否加上了（不支持时 False，不抛异常）。"""
    if not enabled():
        return False
    root = Path(root)
    if IS_MAC:
        return _apply_mac(root)
    if IS_WINDOWS:
        return _apply_windows(root)
    return False


def release(root: str | os.PathLike) -> None:
    """撤掉保护（本程序要改保险箱之前调用）。没加过保护时什么都不做。

    不看 enabled()：关掉开关之前加上的保护也要能撤掉。"""
    root = Path(root)
    unhold(root)
    if IS_MAC:
        _release_mac(root)
    elif IS_WINDOWS:
        _release_windows(root)


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
    prefix = _hold_key(path)
    with _holds_lock:
        keys = [k for k in _holds if k == prefix or k.startswith(prefix.rstrip("\\/") + os.sep)]
    for k in keys:
        _unhold_key(k)
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
    leaked = []
    # 移动 / 改名：万一成功了马上挪回来，后面几项才测得准
    for name, dest in (
        ("移动文件夹", Path(elsewhere) / (root.name + "-moved")),
        ("改名文件夹", root.with_name(root.name + "-renamed")),
    ):
        try:
            os.rename(root, dest)
        except OSError:
            continue
        leaked.append(name)
        os.rename(dest, root)
    # 删除：被拦住会抛 OSError；成功了就是真删了（自检只在临时保险箱上跑）
    for name, fn in (
        ("删除说明文件", lambda: os.unlink(root / C.MARKER_NAME)),
        ("删除 header", lambda: os.unlink(root / C.VAULT_DIRNAME / C.HEADER_NAME)),
        ("删除整个 .vault", lambda: shutil.rmtree(root / C.VAULT_DIRNAME)),
    ):
        try:
            fn()
        except OSError:
            continue
        leaked.append(name)
    return leaked


# ---------------------------------------------------------------- Windows：占用句柄（见模块说明第 3 条）

import threading  # noqa: E402

_holds: dict[str, int] = {}
_holds_lock = threading.Lock()
_holds_enabled = False


def enable_holds(on: bool = True) -> None:
    """图形界面启动时打开。命令行 / 测试默认不持有句柄（进程很快退出，也免得删不掉临时目录）。"""
    global _holds_enabled
    _holds_enabled = on
    if not on:
        unhold_all()


def _hold_key(root: str | os.PathLike) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(root)))


def _win_open_dir_no_share_delete(path: str) -> int | None:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = k32.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    FILE_LIST_DIRECTORY = 0x0001  # 要有「数据」类访问，共享检查才算数（只要属性访问的句柄不参与）
    SYNCHRONIZE = 0x00100000
    FILE_SHARE_READ, FILE_SHARE_WRITE = 0x1, 0x2  # 故意不给 FILE_SHARE_DELETE
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # 打开目录必须带
    from .fsutil import long_path

    h = create(
        long_path(path), FILE_LIST_DIRECTORY | SYNCHRONIZE, FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None,
    )
    invalid = ctypes.c_void_p(-1).value
    if not h or h == invalid:
        return None
    return int(h)


def _win_close(h: int) -> None:
    import ctypes

    ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(h))


def hold(root: str | os.PathLike) -> bool:
    """对已上锁的保险箱根目录持有占用句柄。只在 Windows + 图形界面里生效；重复调用没关系。"""
    if not (IS_WINDOWS and _holds_enabled and enabled()):
        return False
    key = _hold_key(root)
    with _holds_lock:
        if key in _holds:
            return True
        h = _win_open_dir_no_share_delete(key)
        if h is None:
            return False
        _holds[key] = h
        return True


def _unhold_key(key: str) -> bool:
    with _holds_lock:
        h = _holds.pop(key, None)
    if h is None:
        return False
    _win_close(h)
    return True


def unhold(root: str | os.PathLike) -> bool:
    """放掉占用句柄（本程序要动这个保险箱之前 / 从列表移除时）。返回之前是否持有。"""
    if not _holds:
        return False
    return _unhold_key(_hold_key(root))


def unhold_all() -> None:
    with _holds_lock:
        keys = list(_holds)
    for k in keys:
        _unhold_key(k)


def is_held(root: str | os.PathLike) -> bool:
    return _hold_key(root) in _holds


def sync_holds(locked_roots: "list[str | os.PathLike]") -> None:
    """让持有的句柄正好对应这些已上锁的保险箱（界面刷新列表时调用）。"""
    want = {_hold_key(r): r for r in locked_roots}
    with _holds_lock:
        stale = [k for k in _holds if k not in want]
    for k in stale:
        _unhold_key(k)
    for r in want.values():
        hold(r)
