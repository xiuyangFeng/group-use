"""退出流程选项：有托盘才能选"留在后台"，否则藏起来会找不回来。"""

from __future__ import annotations

from gui.quit_flow import quit_choices


def test_quit_choices_with_tray():
    choices = quit_choices(True)
    keys = [k for k, _ in choices]
    assert keys == ["background", "quit", "cancel"]
    labels = " ".join(label for _, label in choices)
    assert "留在后台" in labels
    assert "保持明文" in labels
    assert "取消" in labels


def test_quit_choices_without_tray():
    choices = quit_choices(False)
    keys = [k for k, _ in choices]
    assert keys == ["quit", "cancel"]
    assert "background" not in keys
