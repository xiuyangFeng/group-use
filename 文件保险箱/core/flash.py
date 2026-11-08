"""闪电锁：不加密，几十上百 GB 也是 1 秒左右锁上 / 打开。

做法：上锁时把文件夹里的每一项（文件 / 子文件夹，只动最上面一层）**改名**挪进
.vault/stash/，名字换成随机串；原名字用保险箱密钥加密后记在 .vault/flash.json。
同一个磁盘里改名不搬数据，所以和文件多大无关。然后和加密保险箱一样加系统保护
（防移动 / 防删除，见 core/protect.py），stash 目录额外禁止列出内容、在 Windows 上隐藏。
解锁要密码（和加密保险箱同一套密码 / 密保），把每一项改名挪回原处。

**不是加密**：硬盘上还是原样的文件。拆下硬盘、换个系统、或者懂行的人用管理员权限，
都能直接看到内容（只是名字乱了、要先找到 .vault/stash）。只防「同一台电脑上随手翻看」。

断电 / 中途失败的安全性：
- 先把对照表完整写好（fsync）再开始挪；每一项的改名是原子的；
- 每一项挪没挪，看 stash 里那个随机名字在不在就知道，所以恢复时向哪个方向都能接着做完，
  重复做也没关系（见 recover）。
- 解锁时原位置已经有同名的东西（锁着期间有人在 U 盘等没保护的盘上新建了），
  不覆盖，改名成「名字（闪电锁恢复 1）」。
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from . import constants as C
from . import crypto, fsutil
from .errors import Cancelled, IntegrityError, VaultError, VaultStateError
from .fsutil import long_path

if TYPE_CHECKING:  # pragma: no cover
    from .control import OperationControl
    from .vault import ProgressFn, Vault

MAP_VERSION = 1


# ---------------------------------------------------------------- 对照表

def stash_dir(v: "Vault") -> Path:
    return v.vault_dir / C.STASH_DIRNAME


def map_path(v: "Vault") -> Path:
    return v.vault_dir / C.FLASH_MAP_NAME


def _aad(v: "Vault", sid: str) -> bytes:
    return f"fdv1|flash|{v.header.vault_id}|{sid}".encode("utf-8")


def _save_map(v: "Vault", items: list[tuple[str, str]]) -> None:
    """items = [(随机名字, 原名字)]。原名字加密（看 flash.json 也不知道里面是什么）。"""
    key = v._key
    assert key is not None
    entries = []
    for sid, name in items:
        nonce, ct = crypto.aead_seal(key, name.encode("utf-8", "surrogatepass"), _aad(v, sid))
        entries.append({"id": sid, "n": crypto.b64e(nonce), "c": crypto.b64e(ct)})
    fsutil.atomic_write_json(map_path(v), {"version": MAP_VERSION, "entries": entries}, mode=0o600)


def _load_map(v: "Vault") -> list[tuple[str, str]]:
    p = map_path(v)
    if not p.exists():
        return []
    try:
        data = json.loads(Path(long_path(p)).read_text(encoding="utf-8"))
        entries = data["entries"]
    except Exception as exc:  # noqa: BLE001
        raise IntegrityError(f"闪电锁对照表（{C.FLASH_MAP_NAME}）读不出来：{exc}")
    key = v._key
    assert key is not None
    out = []
    for e in entries:
        sid = str(e["id"])
        try:
            name = crypto.aead_open(key, crypto.b64d(e["n"]), crypto.b64d(e["c"]), _aad(v, sid))
        except Exception:  # noqa: BLE001
            raise IntegrityError("闪电锁对照表被改动过（解不开），请不要动 .vault 里的东西")
        n = name.decode("utf-8", "surrogatepass")
        if not _safe_name(n) or not _safe_name(sid):
            raise IntegrityError(f"闪电锁对照表里有不合法的名字：{n!r}")
        out.append((sid, n))
    return out


def _safe_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name and "\0" not in name


def _top_items(v: "Vault") -> list[str]:
    """文件夹最上面一层要藏起来的东西（.vault 和说明文件除外）。"""
    out = []
    for name in sorted(os.listdir(long_path(v.root))):
        if name in (C.VAULT_DIRNAME, C.MARKER_NAME):
            continue
        out.append(name)
    return out


def _fsync_dirs(v: "Vault") -> None:
    fsutil.fsync_dir(v.root)
    fsutil.fsync_dir(stash_dir(v))


# ---------------------------------------------------------------- 上锁

def _busy_check(v: "Vault") -> None:
    """macOS 上改名不受打开的文件影响，但打开着的程序（Word 等）之后会往原位置存盘，
    所以先查。Windows 上被打开的文件夹改不了名，改名那一步自然会失败并回滚。"""
    if os.environ.get("FDVAULT_SKIP_BUSY_CHECK") == "1":
        return
    busy = fsutil.busy_under(v.root, {C.VAULT_DIRNAME})
    if not busy:
        return
    names = "、".join(f"「{Path(p).name}」" for p in busy[:5])
    more = "" if len(busy) <= 5 else f" 等 {len(busy)} 个文件"
    raise VaultError(
        f"{names}{more}正在被其他程序使用，现在不能上锁。\n"
        "请先关闭打开这些文件的软件（比如 Excel、看图工具）再试。\n"
        "保险箱没有改动，文件还是原样。"
    )


def lock(v: "Vault", progress: "ProgressFn | None", control: "OperationControl | None", *, busy_check: bool = True) -> dict:
    """调用方：已拿到互斥锁、撤掉保护、打开密钥、header 状态已设为 LOCKING。"""
    if busy_check:
        _busy_check(v)
    fsutil.ensure_dir(stash_dir(v), 0o700)
    if _load_map(v):
        raise VaultStateError("上次的闪电锁还没打开完，请先「恢复」")
    names = _top_items(v)
    items = [(secrets.token_hex(16), n) for n in names]
    total = len(items)
    v._emit(progress, "正在上锁", total_files=total)
    from . import crash

    crash.point("flash:before_map")
    _save_map(v, items)  # 先记下来再挪：断电后知道每一项去了哪
    crash.point("flash:after_map")

    moved = 0
    try:
        for sid, name in items:
            if control is not None:
                control.check()
            crash.point("flash:before_move")
            os.rename(long_path(v.root / name), long_path(stash_dir(v) / sid))
            moved += 1
            if moved % 64 == 0:
                v._emit(progress, "正在上锁", done_files=moved, total_files=total)
    except Cancelled:
        raise  # 留在 LOCKING：调用方 recover(rollback=True) 挪回去
    except OSError as exc:
        _move_back(v, items[:moved])
        _fsync_dirs(v)
        os.unlink(long_path(map_path(v)))
        name = items[moved][1]
        raise _friendly_move_error(name, exc)
    _fsync_dirs(v)
    crash.point("flash:after_all_moved")
    v._emit(progress, "上锁完成", done_files=total, total_files=total)
    return {"files": total, "dirs": 0, "bytes": 0, "skipped": []}


def _friendly_move_error(name: str, exc: OSError) -> VaultError:
    win = getattr(exc, "winerror", None)
    if win in (5, 32, 33) or isinstance(exc, PermissionError):
        return VaultError(
            f"「{name}」里有文件正在被其他程序使用（或没有权限），现在不能上锁。\n"
            "请先关闭打开这些文件的软件再试。\n文件还是原样，没有改动。"
        )
    return VaultError(f"挪动「{name}」失败：{exc}\n文件还是原样，没有改动。")


# ---------------------------------------------------------------- 解锁

def _target_for(v: "Vault", name: str, taken: set[str]) -> Path:
    """原位置被占了就换个名字，绝不覆盖。taken：这次已经用掉的名字。"""
    p = v.root / name
    if not os.path.lexists(long_path(p)) and name not in taken:
        return p
    stem, ext = os.path.splitext(name)
    if not stem:  # ".DS_Store" 这类
        stem, ext = name, ""
    i = 1
    while True:
        cand = f"{stem}（闪电锁恢复 {i}）{ext}"
        p = v.root / cand
        if not os.path.lexists(long_path(p)) and cand not in taken:
            return p
        i += 1


def _move_back(v: "Vault", items: list[tuple[str, str]]) -> list[str]:
    """把还在 stash 里的挪回原处。返回因为重名而换了名字的项。"""
    renamed = []
    taken: set[str] = set()
    for sid, name in items:
        src = stash_dir(v) / sid
        if not os.path.lexists(long_path(src)):
            continue  # 已经挪回去了
        dst = _target_for(v, name, taken)
        taken.add(dst.name)
        from . import crash

        crash.point("flash:before_move_back")
        os.rename(long_path(src), long_path(dst))
        if dst.name != name:
            renamed.append(f"「{name}」→「{dst.name}」")
    return renamed


def unlock(v: "Vault", progress: "ProgressFn | None", control: "OperationControl | None") -> dict:
    """调用方：已拿到互斥锁、撤掉保护、打开密钥、header 状态已设为 UNLOCKING。
    解锁一旦开始就做完（没有取消点）：反正只要一眨眼。"""
    items = _load_map(v)
    missing = [n for sid, n in items if not os.path.lexists(long_path(stash_dir(v) / sid))]
    if missing and len(missing) == len(items) and not _all_back(v, items):
        raise IntegrityError(
            f"闪电锁里的 {len(missing)} 项都找不到了（.vault/stash 被动过），请先备份整个文件夹再排查"
        )
    total = len(items)
    v._emit(progress, "正在解锁", total_files=total)
    renamed = _move_back(v, items)
    _fsync_dirs(v)
    from . import crash

    crash.point("flash:before_map_unlink")
    os.unlink(long_path(map_path(v)))
    fsutil.fsync_dir(v.vault_dir)
    v._emit(progress, "解锁完成", done_files=total, total_files=total)
    return {"files": total, "dirs": 0, "bytes": 0, "renamed": renamed}


def _all_back(v: "Vault", items: list[tuple[str, str]]) -> bool:
    return all(os.path.lexists(long_path(v.root / n)) for _sid, n in items)


# ---------------------------------------------------------------- 恢复 / 校验

def recover(v: "Vault", target_lock: bool, progress: "ProgressFn | None") -> dict:
    """中断后接着做完（target_lock=True 继续上锁，False 全部挪回 = 继续解锁 / 回滚上锁）。"""
    items = _load_map(v)
    if target_lock:
        if not items:
            # 对照表还没写好就断了：一项都没挪，从头上锁
            return lock(v, progress, None, busy_check=False)
        total = len(items)
        for sid, name in items:
            dst = stash_dir(v) / sid
            src = v.root / name
            if os.path.lexists(long_path(dst)) or not os.path.lexists(long_path(src)):
                continue
            os.rename(long_path(src), long_path(dst))
        _fsync_dirs(v)
        return {"files": total, "dirs": 0, "bytes": 0, "skipped": []}
    total = len(items)
    renamed = _move_back(v, items) if items else []
    if items:
        _fsync_dirs(v)
    if map_path(v).exists():
        os.unlink(long_path(map_path(v)))
    return {"files": total, "dirs": 0, "bytes": 0, "renamed": renamed}


def verify(v: "Vault") -> dict:
    items = _load_map(v)
    problems = [
        {"path": name, "reason": "找不到了"}
        for sid, name in items
        if not os.path.lexists(long_path(stash_dir(v) / sid))
    ]
    return {"ok": not problems, "files": len(items), "problems": problems}


def stash_is_empty(v: "Vault") -> bool:
    d = stash_dir(v)
    try:
        return not any(True for _ in os.scandir(long_path(d)))
    except FileNotFoundError:
        return True
    except OSError:
        return False  # 列不出来（还加着保护）：当成不空，宁可不删
