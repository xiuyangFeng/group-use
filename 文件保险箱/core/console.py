"""控制台编码兼容：Windows 控制台/管道可能是 cp1252、cp936 等代码页，
直接 print 中文或 ✓/✗ 会抛 UnicodeEncodeError。启动时把 stdout/stderr 切到 UTF-8
（errors="replace" 兜底），这样 CLI、构建脚本、CI 在 Windows 上不会因此失败。

注意：不改 stdin。`--password-stdin` 读的是控制台默认编码，强制 UTF-8 反而会把
中文密码解错；交互密码用 getpass，Python 在 Windows 上走宽字符 API。
"""

from __future__ import annotations

import os
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


def hard_exit(code: int = 0) -> None:
    """退出进程，Windows 上绕过 DLL detach。

    在 Windows 上创建过 Qt 组件后调用 os._exit，进程退出时 Qt DLL 的 detach
    偶尔会 access violation（退出码 3221225477），把已经通过的检查变成失败。
    TerminateProcess 不执行 DllMain 的 PROCESS_DETACH，可稳定拿到想要的退出码。
    """
    if sys.platform == "win32":
        try:
            import ctypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            k32.TerminateProcess.restype = ctypes.c_int
            k32.TerminateProcess(k32.GetCurrentProcess(), code)
        except Exception:  # noqa: BLE001
            pass
    os._exit(code)
