"""退出流程的选项（纯逻辑，方便单测；不依赖 Qt）。

为什么单独一个模块：退出时如果自动上锁失败，用户有三个选择：
- 留在后台（继续自动上锁/重试）—— 推荐，明文不落地外泄的风险最小；
- 仍然退出（保持明文）—— 明确知情后的兜底；
- 取消 —— 回去处理占用。
没有托盘入口时不能提供"留在后台"，否则窗口藏起来就找不回来了。
"""

from __future__ import annotations


def quit_choices(has_tray: bool) -> list[tuple[str, str]]:
    """返回 [(key, label), ...]，顺序即按钮顺序。"""
    choices: list[tuple[str, str]] = []
    if has_tray:
        choices.append(("background", "留在后台（继续自动上锁）"))
    choices.append(("quit", "仍然退出（保持明文）"))
    choices.append(("cancel", "取消"))
    return choices
