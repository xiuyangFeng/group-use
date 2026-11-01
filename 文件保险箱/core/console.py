"""控制台编码兼容：Windows 控制台/管道可能是 cp1252、cp936 等代码页，
直接 print 中文或 ✓/✗ 会抛 UnicodeEncodeError。启动时把 stdout/stderr 切到 UTF-8
（errors="replace" 兜底），这样 CLI、构建脚本、CI 在 Windows 上不会因此失败。

注意：不改 stdin。`--password-stdin` 读的是控制台默认编码，强制 UTF-8 反而会把
中文密码解错；交互密码用 getpass，Python 在 Windows 上走宽字符 API。
"""

from __future__ import annotations

import sys


def setup_utf8_stdio() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
