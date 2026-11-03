"""外部程序"正在使用"标记（目前只有 CT 脱敏工具会写）。

约定（两边共同遵守，改动时两个项目一起改）：

- 位置：保险箱根目录下 ``.vault/in-use-<工具>.json``。放在 ``.vault`` 里，
  所以既不会被加密，也不会出现在用户的文件夹里。
- 内容：``{"tool": 显示名, "active": true/false, "pid": ..., "host": ...,
  "started_at": ISO 时间, "heartbeat": Unix 秒, "what": "原始数据、映射表"}``。
- 外部程序运行期间每 30 秒刷新一次 heartbeat，结束时把 active 写成 false
  （脱敏工具承诺不删除任何文件，所以标记不删，只改成"已结束"）。
- 只有 active 为 true、且 heartbeat 在 ``INUSE_STALE_SECONDS`` 以内的标记才算数：
  外部程序崩溃或被强制结束后，最多几分钟就自动失效，不会把保险箱永远卡住。

本模块只读，不需要密码。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from . import constants as C


def active_users(root: str | Path, *, now: float | None = None) -> list[dict]:
    """返回当前正在使用这个保险箱的外部程序（读不出来的标记直接忽略）。"""
    vault_dir = Path(root) / C.VAULT_DIRNAME
    now = time.time() if now is None else now
    out: list[dict] = []
    try:
        markers = sorted(vault_dir.glob(C.INUSE_GLOB))
    except OSError:
        return out
    for p in markers:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            beat = float(data.get("heartbeat", 0))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if not isinstance(data, dict) or data.get("active") is not True:
            continue
        # 允许一点时钟误差；心跳太旧（程序已经不在了）就不算
        if -C.INUSE_CLOCK_SKEW_SECONDS <= now - beat <= C.INUSE_STALE_SECONDS:
            out.append(data)
    return out


def describe(users: list[dict]) -> str:
    """给人看的一句话：「CT 数据脱敏工具」（14:05 开始，用到 原始数据、映射表）。"""
    parts = []
    for u in users:
        name = str(u.get("tool") or "其他程序")
        extra = []
        started = str(u.get("started_at") or "")
        try:
            extra.append(f"{datetime.fromisoformat(started):%H:%M} 开始")
        except ValueError:
            pass
        if u.get("what"):
            extra.append(f"用到 {u['what']}")
        parts.append(f"「{name}」" + (f"（{'，'.join(extra)}）" if extra else ""))
    return "、".join(parts)
