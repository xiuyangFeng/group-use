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
            if Path(dirpath) == vault_dir and C.STASH_DIRNAME in dirnames:
                # 闪电锁：只保护 stash 目录本身，不进去逐个处理用户的文件（几十万个也要 1 秒内锁上）
                dirnames.remove(C.STASH_DIRNAME)
                dirs.append(vault_dir / C.STASH_DIRNAME)
            dirs.append(Path(dirpath))
            files.extend(Path(dirpath) / f for f in filenames)
    return dirs, files


def _stash(root: Path) -> Path:
    return root / C.VAULT_DIRNAME / C.STASH_DIRNAME


def _is_flash(root: Path) -> bool:
    """闪电锁保险箱一定有 .vault/stash（建的时候就建好），加密保险箱没有。"""
    p = _stash(root)
    return p.is_dir() and not p.is_symlink()


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
    if _is_flash(root):
        ok = _apply_windows_flash(root)
    else:
        ok = _run(["icacls", str(root), "/deny", WIN_DENY, "/C", "/Q"])
    parent = root.parent
    if ok and parent != root:
        existing = _win_everyone_denies(parent)
        if not existing:
            # 只改父目录自己（不用 icacls：它会把父目录下整棵树的权限重写一遍，桌面上东西多时很慢）
            _win_add_deny(parent, WIN_MASK_DELETE_CHILD)
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
    if _is_flash(root):
        _release_windows_flash(root)
    # 没加过规则就别调 /remove:d：它照样会重写整棵树的继承权限，每个文件的「状态改变时间」
    # 都会变，增量上锁就会把没改过的文件全当成改过的重新加密（文件多时也很慢）。
    # 读不到（None）时宁可照旧撤一次。
    elif _win_everyone_denies_or_none(root) != []:
        _run(["icacls", str(root), "/remove:d", WIN_EVERYONE, "/C", "/Q"])
    parent = root.parent
    if parent == root:
        return
    if _win_is_our_parent_rule(_win_everyone_denies(parent)) and not _win_locked_siblings(parent, root):
        _win_remove_denies(parent)


# ---------------------------------------------------------------- Windows：只改单个对象的权限（不往下传）
#
# icacls / SetNamedSecurityInfo 改一个目录的权限时会把整棵子树重算一遍（几十万个文件要很久，
# 还会改掉每个文件的「状态改变时间」）。老接口 SetFileSecurityW 只改这一个对象，不往下传。
# 闪电锁只在少数几个对象上加「不可继承」的拒绝规则，正好用它：1 秒内加完 / 撤完。

WIN_MASK_LIST = 0x0001          # FILE_LIST_DIRECTORY：列出目录内容
WIN_MASK_DELETE_CHILD = 0x0040  # FILE_DELETE_CHILD
WIN_MASK_DELETE = 0x10000       # DELETE
WIN_FLASH_DIR = WIN_MASK_DELETE | WIN_MASK_DELETE_CHILD
WIN_FLASH_STASH = WIN_FLASH_DIR | WIN_MASK_LIST
WIN_FLASH_FILE = WIN_MASK_DELETE
_OUR_MASKS = {WIN_FLASH_DIR, WIN_FLASH_STASH, WIN_FLASH_FILE, WIN_MASK_DELETE_CHILD}

# SDDL 里权限可能写成字母（按目录服务权限的名字），换算回数值
_SDDL_RIGHTS = {
    "CC": 0x1, "DC": 0x2, "LC": 0x4, "SW": 0x8, "RP": 0x10, "WP": 0x20, "DT": 0x40, "LO": 0x80,
    "CR": 0x100, "SD": 0x10000, "RC": 0x20000, "WD": 0x40000, "WO": 0x80000,
    "GA": 0x10000000, "GX": 0x20000000, "GW": 0x40000000, "GR": 0x80000000,
    "FA": 0x1F01FF, "FR": 0x120089, "FW": 0x120116, "FX": 0x1200A0,
}


def _sddl_mask(text: str) -> int | None:
    text = text.strip()
    if text.lower().startswith("0x"):
        try:
            return int(text, 16)
        except ValueError:
            return None
    if len(text) % 2:
        return None
    total = 0
    for i in range(0, len(text), 2):
        v = _SDDL_RIGHTS.get(text[i : i + 2].upper())
        if v is None:
            return None
        total |= v
    return total


def _split_dacl(sddl: str) -> tuple[str, str, list[str], str] | None:
    """把 SDDL 拆成 (D: 之前, D:标志, [ACE...], 之后)。只认识 DACL 部分。"""
    i = sddl.find("D:")
    if i < 0:
        return None
    j = i + 2
    while j < len(sddl) and sddl[j] not in "(":
        if sddl[j] == ":":  # 下一段（S: 等）
            break
        j += 1
    flags = sddl[i + 2 : j]
    aces: list[str] = []
    k = j
    while k < len(sddl) and sddl[k] == "(":
        end = sddl.find(")", k)
        if end < 0:
            return None
        aces.append(sddl[k : end + 1])
        k = end + 1
    return sddl[:i], flags, aces, sddl[k:]


def _is_our_ace(ace: str) -> bool:
    parts = ace[1:-1].split(";")
    if len(parts) < 6:
        return False
    kind, aflags, rights, _obj, _inh, sid = parts[:6]
    return (
        kind == "D"
        and aflags == ""  # 不可继承、不是继承来的
        and sid.upper() in ("WD", "S-1-1-0")
        and _sddl_mask(rights) in _OUR_MASKS
    )


def _win_api():
    import ctypes
    from ctypes import wintypes

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    adv.GetFileSecurityW.restype = wintypes.BOOL
    adv.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    adv.SetFileSecurityW.restype = wintypes.BOOL
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
    ]
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
    ]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    k32.LocalFree.restype = ctypes.c_void_p
    k32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    k32.SetFileAttributesW.restype = wintypes.BOOL
    k32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    k32.GetFileAttributesW.restype = wintypes.DWORD
    return ctypes, wintypes, adv, k32


_DACL_INFO = 0x4  # DACL_SECURITY_INFORMATION


def _win_get_dacl(path: Path) -> str | None:
    try:
        ctypes, wintypes, adv, k32 = _win_api()
    except (OSError, AttributeError):
        return None
    from .fsutil import long_path

    p = long_path(path)
    need = wintypes.DWORD(0)
    adv.GetFileSecurityW(p, _DACL_INFO, None, 0, ctypes.byref(need))
    if not need.value:
        return None
    buf = ctypes.create_string_buffer(need.value)
    if not adv.GetFileSecurityW(p, _DACL_INFO, buf, need, ctypes.byref(need)):
        return None
    out = ctypes.c_void_p()
    if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(buf, 1, _DACL_INFO, ctypes.byref(out), None):
        return None
    try:
        return ctypes.wstring_at(out.value)
    finally:
        k32.LocalFree(out)


def _win_set_dacl(path: Path, sddl: str) -> bool:
    try:
        ctypes, _wintypes, adv, k32 = _win_api()
    except (OSError, AttributeError):
        return False
    from .fsutil import long_path

    psd = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(psd), None):
        return False
    try:
        return bool(adv.SetFileSecurityW(long_path(path), _DACL_INFO, psd))
    finally:
        k32.LocalFree(psd)


def _win_add_deny(path: Path, mask: int) -> bool:
    """在 path 自己的 DACL 最前面加一条「Everyone 拒绝 mask」（不可继承）。已经有就不重复加。"""
    sddl = _win_get_dacl(path)
    parts = _split_dacl(sddl) if sddl else None
    if parts is None:
        return False
    head, flags, aces, tail = parts
    if any(_is_our_ace(a) and _sddl_mask(a[1:-1].split(";")[2]) == mask for a in aces):
        return True
    aces = [f"(D;;0x{mask:x};;;WD)"] + aces
    return _win_set_dacl(path, f"{head}D:{flags}{''.join(aces)}{tail}")


def _win_remove_denies(path: Path) -> bool:
    """去掉 path 上我们加的那几种拒绝规则（不碰别人的，也不往下传）。"""
    sddl = _win_get_dacl(path)
    parts = _split_dacl(sddl) if sddl else None
    if parts is None:
        return False
    head, flags, aces, tail = parts
    keep = [a for a in aces if not _is_our_ace(a)]
    if len(keep) == len(aces):
        return True
    return _win_set_dacl(path, f"{head}D:{flags}{''.join(keep)}{tail}")


def _win_hide(path: Path) -> None:
    """资源管理器里默认看不见（隐藏 + 系统，「受保护的操作系统文件」）。"""
    try:
        _ctypes, _wt, _adv, k32 = _win_api()
    except (OSError, AttributeError):
        return
    from .fsutil import long_path

    p = long_path(path)
    attrs = k32.GetFileAttributesW(p)
    if attrs == 0xFFFFFFFF:
        return
    k32.SetFileAttributesW(p, attrs | 0x2 | 0x4)  # HIDDEN | SYSTEM


def _flash_objects(root: Path) -> list[tuple[Path, int]]:
    """闪电锁在 Windows 上要加规则的对象：根目录、.vault 和里面的目录、元数据文件、说明文件、stash。"""
    vd = root / C.VAULT_DIRNAME
    out: list[tuple[Path, int]] = [(root, WIN_FLASH_DIR), (vd, WIN_FLASH_DIR)]
    marker = root / C.MARKER_NAME
    if marker.is_file():
        out.append((marker, WIN_FLASH_FILE))
    try:
        children = list(vd.iterdir())
    except OSError:
        children = []
    for c in children:
        if c.name == C.STASH_DIRNAME:
            out.append((c, WIN_FLASH_STASH))
        elif c.name == "instance.lock" or c.is_symlink():
            continue
        elif c.is_dir():
            out.append((c, WIN_FLASH_DIR))
        else:
            out.append((c, WIN_FLASH_FILE))
    return out


def _apply_windows_flash(root: Path) -> bool:
    ok = True
    for p, mask in _flash_objects(root):
        ok = _win_add_deny(p, mask) and ok
    _win_hide(root / C.VAULT_DIRNAME)
    return ok


def _release_windows_flash(root: Path) -> None:
    # 先撤 stash 的（撤之前列不出它里面的东西，但它本身的权限能改），顺序无所谓
    for p, _mask in _flash_objects(root):
        _win_remove_denies(p)


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
    stash = _stash(root)
    if _is_flash(root):
        try:
            os.chmod(stash, 0)  # 闪电锁：谁都列不出、进不去（主人自己也要先改回权限）
        except OSError:
            pass
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
    if _is_flash(root):
        try:
            os.chmod(_stash(root), 0o700)
        except OSError:
            pass
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
        for dirpath, dirnames, _ in os.walk(path, followlinks=False):
            if C.VAULT_DIRNAME in dirnames and _is_flash(Path(dirpath)):
                _release_windows_flash(Path(dirpath))
                _win_remove_denies(Path(dirpath).parent)
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
