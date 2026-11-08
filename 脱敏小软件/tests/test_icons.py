"""程序图标：打包脚本和界面都依赖 deid/icons 里的这几个文件。"""

from pathlib import Path

ICONS = Path(__file__).resolve().parent.parent / "deid" / "icons"


def test_icon_files_present():
    for name in ["app.ico", "app.icns", "mac-32.png", "mac-64.png", "mac-256.png", "mac-1024.png"]:
        p = ICONS / name
        assert p.is_file() and p.stat().st_size > 0, p
    assert (ICONS / "app.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"  # ICO 文件头
    assert (ICONS / "app.icns").read_bytes()[:4] == b"icns"


def test_set_app_icon_does_not_crash():
    import pytest

    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("没有图形环境")
    try:
        root.withdraw()
        from deid.gui import set_app_icon

        set_app_icon(root)
    finally:
        root.destroy()
