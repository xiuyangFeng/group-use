"""配置存储 + 空闲时间检测。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from core import fsutil

APP_NAME = "文件保险箱"
DEFAULT_AUTO_LOCK_MIN = 15
AUTO_LOCK_NEVER = 0  # 不按空闲时间自动上锁（退出程序 / 注销 / 关机时照样会先上锁）
AUTO_LOCK_CHOICES = (1, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 480, AUTO_LOCK_NEVER)


def auto_lock_label(minutes: int) -> str:
    """下拉框 / 菜单里显示的文字。"""
    if minutes == AUTO_LOCK_NEVER:
        return "从不（只在退出时上锁）"
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    if minutes > 60:
        return f"{minutes // 60} 小时 {minutes % 60} 分钟"
    return f"{minutes} 分钟"


def app_data_dir() -> Path:
    return fsutil.platform_data_dir()


class AppStore:
    """最近保险箱列表 + 设置，存到用户数据目录，不联网。"""

    def __init__(self, config_dir: Path | None = None):
        self.config_dir = Path(config_dir).expanduser() if config_dir else app_data_dir()
        self.path = self.config_dir / "config.json"
        self.data: dict = {
            "version": 1,
            "vaults": [],
            "auto_lock_minutes": DEFAULT_AUTO_LOCK_MIN,
            "minimize_to_tray": True,
            "tray_hint_shown": False,
            "last_vault_dir": str(Path.home() / "Documents"),
        }
        self.load()

    # ---------------------------------------------------------------- 读写

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            if isinstance(raw, dict):
                self.data.update(raw)
        except (OSError, ValueError):
            pass
        self.data["vaults"] = [v for v in self.data.get("vaults", []) if isinstance(v, dict) and v.get("path")]
        if self.data.get("auto_lock_minutes") not in AUTO_LOCK_CHOICES:
            self.data["auto_lock_minutes"] = DEFAULT_AUTO_LOCK_MIN
        for v in self.data["vaults"]:
            # 单个保险箱自己的时间：认不出的值当作「跟随默认」
            if "auto_lock_minutes" in v and v["auto_lock_minutes"] not in AUTO_LOCK_CHOICES:
                v.pop("auto_lock_minutes")

    def save(self) -> None:
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            fsutil.atomic_write_json(self.path, self.data, mode=0o600)
        except OSError:
            pass

    # ---------------------------------------------------------------- 保险箱列表

    @property
    def vaults(self) -> list[dict]:
        return self.data["vaults"]

    def vault_paths(self) -> list[str]:
        return [v["path"] for v in self.vaults]

    @staticmethod
    def _norm(path: str | Path) -> str:
        return str(Path(path).expanduser().resolve())

    def add_vault(self, path: str | Path) -> dict:
        path = self._norm(path)
        for v in self.vaults:
            if v["path"] == path:
                return v
        ref = {"path": path, "added_at": _now(), "last_unlock_at": "", "last_unlock_by": "", "last_lock_at": ""}
        self.vaults.insert(0, ref)
        self.save()
        return ref

    def remove_vault(self, path: str) -> None:
        path = self._norm(path)
        self.data["vaults"] = [v for v in self.vaults if v["path"] != path]
        self.save()

    def get(self, path: str) -> dict | None:
        path = self._norm(path)
        for v in self.vaults:
            if v["path"] == path:
                return v
        return None

    def touch_unlock(self, path: str, actor: str = "") -> None:
        ref = self.get(path)
        if ref is not None:
            ref["last_unlock_at"] = _now()
            ref["last_unlock_by"] = actor
            self.save()

    def touch_lock(self, path: str) -> None:
        ref = self.get(path)
        if ref is not None:
            ref["last_lock_at"] = _now()
            self.save()

    # ---------------------------------------------------------------- 设置

    @property
    def auto_lock_minutes(self) -> int:
        return int(self.data.get("auto_lock_minutes", DEFAULT_AUTO_LOCK_MIN))

    def set_auto_lock(self, minutes: int) -> None:
        minutes = int(minutes)
        if minutes not in AUTO_LOCK_CHOICES:
            raise ValueError(f"不支持的自动上锁时间：{minutes}")
        self.data["auto_lock_minutes"] = minutes
        self.save()

    # 每个保险箱可以单独设自动上锁时间；没设的跟随上面的默认值

    def vault_auto_lock(self, path: str) -> int | None:
        """这个保险箱单独设的时间；None 表示跟随默认。"""
        ref = self.get(path)
        if ref is None or "auto_lock_minutes" not in ref:
            return None
        return int(ref["auto_lock_minutes"])

    def effective_auto_lock(self, path: str) -> int:
        own = self.vault_auto_lock(path)
        return self.auto_lock_minutes if own is None else own

    def set_vault_auto_lock(self, path: str, minutes: int | None) -> None:
        """minutes 为 None：改回跟随默认。"""
        ref = self.get(path)
        if ref is None:
            raise KeyError(path)
        if minutes is None:
            ref.pop("auto_lock_minutes", None)
        else:
            minutes = int(minutes)
            if minutes not in AUTO_LOCK_CHOICES:
                raise ValueError(f"不支持的自动上锁时间：{minutes}")
            ref["auto_lock_minutes"] = minutes
        self.save()

    @property
    def minimize_to_tray(self) -> bool:
        return bool(self.data.get("minimize_to_tray", True))

    def set_minimize_to_tray(self, value: bool) -> None:
        self.data["minimize_to_tray"] = bool(value)
        self.save()

    @property
    def audit_path(self) -> Path:
        return self.config_dir / "audit.jsonl"

    @property
    def log_path(self) -> Path:
        return self.config_dir / "gui.log"

    @property
    def last_vault_dir(self) -> str:
        return str(self.data.get("last_vault_dir") or Path.home())

    def set_last_vault_dir(self, path: str) -> None:
        self.data["last_vault_dir"] = str(path)
        self.save()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 空闲时间

_last_idle = 0.0
_last_idle_at = 0.0


def idle_seconds() -> float | None:
    """距离上次键盘/鼠标操作多少秒；拿不到就返回 None（此时不做自动上锁）。"""
    global _last_idle, _last_idle_at
    now = time.monotonic()
    if now - _last_idle_at < 0.8:  # 1 秒内复用，减少系统调用
        return _last_idle
    value: float | None = None
    try:
        if sys.platform == "darwin":
            value = _idle_macos()
        elif sys.platform == "win32":
            value = _idle_windows()
        elif sys.platform.startswith("linux"):
            value = _idle_linux()
    except Exception:  # noqa: BLE001 - 拿不到空闲时间不算错误
        value = None
    if value is not None:
        _last_idle = value
        _last_idle_at = now
    return value


def _idle_macos() -> float | None:
    out = subprocess.run(
        ["ioreg", "-c", "IOHIDSystem", "-d", "1", "-r"],
        capture_output=True,
        text=True,
        timeout=2,
    ).stdout
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    if not m:
        return None
    return int(m.group(1)) / 1e9


def _idle_windows() -> float | None:
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return None
    return (ctypes.windll.kernel32.GetTickCount64() - lii.dwTime) / 1000.0


def _idle_linux() -> float | None:
    # 仅开发环境兜底：优先 XScreenSaver，其次 xprintidle
    try:
        out = subprocess.run(["xprintidle"], capture_output=True, text=True, timeout=2).stdout.strip()
        return int(out) / 1000.0
    except (OSError, ValueError):
        return None
