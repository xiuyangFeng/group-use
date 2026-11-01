"""崩溃点注入：用于测试「任意一步断电都不丢数据」。

用法（只给测试用）：
    FDVAULT_CRASH_AT="lock:before_unlink_plaintext=3,unlock:after_rename=1"

格式为逗号分隔的「点名=第几次调用时崩溃」。命中时直接 os._exit(70) 模拟断电。
"""

from __future__ import annotations

import os
import threading

_EXIT_CODE = 70
_lock = threading.Lock()
_counters: dict[str, int] = {}
_specs: dict[str, int] | None = None


def _parse() -> dict[str, int]:
    global _specs
    raw = os.environ.get("FDVAULT_CRASH_AT", "").strip()
    specs: dict[str, int] = {}
    if not raw:
        _specs = specs
        return specs
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"FDVAULT_CRASH_AT 格式错误：{item!r}")
        point, _, n = item.partition("=")
        specs[point.strip()] = int(n)
    _specs = specs
    return specs


def point(name: str) -> None:
    """在关键步骤调用。命中注入点时直接退出进程（模拟断电）。"""
    spec = _specs if _specs is not None else _parse()
    if not spec or name not in spec:
        return
    with _lock:
        _counters[name] = _counters.get(name, 0) + 1
        hit = _counters[name] == spec[name]
    if hit:
        os._exit(_EXIT_CODE)


def reset_for_tests() -> None:
    global _specs
    with _lock:
        _counters.clear()
        _specs = None
