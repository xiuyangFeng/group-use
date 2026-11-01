"""图形界面入口：python -m gui"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtGui import QFileOpenEvent
from PySide6.QtWidgets import QApplication

from core.console import setup_utf8_stdio

from .main_window import MainWindow
from .store import AppStore, app_data_dir
from .style import apply_theme, make_app_icon
from .workers import SingleInstance


def folder_arg(argv: list[str]) -> str | None:
    """从命令行参数里取文件夹（右键菜单 / 命令行启动时传入）。"""
    for raw in argv[1:]:
        if raw.startswith("-"):
            continue
        candidate = Path(raw)
        if candidate.is_dir():
            return str(candidate.resolve())
    return None


def folder_from_event(event) -> str | None:
    """从 QFileOpenEvent 里取文件夹路径；不是打开文件夹的事件就返回 None。"""
    try:
        if event.type() != QEvent.FileOpen:
            return None
        url = event.url()
        path = url.toLocalFile() if hasattr(url, "toLocalFile") else ""
        if path and Path(path).is_dir():
            return str(Path(path).resolve())
    except (AttributeError, TypeError):
        pass  # 不是 Qt 文件打开事件
    except Exception:  # noqa: BLE001
        logging.exception("解析文件打开事件失败")
    return None


class FolderOpenFilter(QObject):
    """macOS：把文件夹拖到程序图标 / 用 `open -a 文件保险箱 文件夹` 时打开新建向导。

    macOS 的 Finder 拖拽和 `open -a` 不会走命令行参数，而是发 QFileOpenEvent。
    """

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        path = folder_from_event(event)
        if path:
            QTimer.singleShot(0, lambda p=path: self._window.new_vault(p))
            return True
        return False


def option_value(args: list[str], name: str) -> str | None:
    """取 --name value 或 --name=value；没有就返回 None。"""
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def setup_logging(store: AppStore) -> None:
    try:
        app_data_dir().mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(store.log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    except OSError:
        logging.basicConfig(level=logging.INFO, force=True)

    def hook(exc_type, exc, tb):
        logging.error("未捕获异常", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook


def main(argv: list[str] | None = None) -> int:
    setup_utf8_stdio()
    args = list(sys.argv if argv is None else argv)

    # 版本号（发安装包后排查问题用；不需要打开界面）
    if "--version" in args or "-V" in args:
        from . import __version__

        print(f"文件保险箱 {__version__}")
        return 0

    report = option_value(args, "--report")

    # 诊断包：收集日志/设置/环境信息成 zip（同事反馈问题用，不联网）
    if "--diagnostics" in args:
        from .diagnostics import run as run_diagnostics

        return run_diagnostics(option_value(args, "--out"))

    # 真实参数验收：默认 KDF + 默认分块，端到端跑加密流程（安装包验收用）
    if "--acceptance" in args:
        from .acceptance_check import run as run_acceptance

        return run_acceptance(report)

    # 打包自检：不打开界面，验证核心 + 界面组件都能用（安装包的健康检查）
    if "--selftest" in args:
        from .selfcheck import run as run_selfcheck

        return run_selfcheck(report)

    app = QApplication(args)
    app.setApplicationName("文件保险箱")
    app.setApplicationDisplayName("文件保险箱")
    app.setOrganizationName("组内工具")
    app.setQuitOnLastWindowClosed(False)
    apply_theme(app)
    app.setWindowIcon(make_app_icon())

    store = AppStore()
    setup_logging(store)

    single = SingleInstance()
    if single.activate_existing():
        print("文件保险箱已经在运行，已唤起现有窗口。")
        return 0

    window = MainWindow(store)
    single.start_server(window.show_and_raise)
    window.show()
    window.raise_()
    window.activateWindow()

    # 打包冒烟测试用：FDVAULT_AUTO_QUIT_MS=3000 时 3 秒后强制退出（正常使用不设置）。
    # 必须在打开新建向导之前挂上，否则向导是模态 exec()，冒烟会一直卡住。
    auto_quit = os.environ.get("FDVAULT_AUTO_QUIT_MS")
    if auto_quit:

        def _auto_quit() -> None:
            try:
                window.close()
            finally:
                logging.shutdown()
                os._exit(0)

        QTimer.singleShot(int(auto_quit), _auto_quit)

    # 右键菜单 / 拖到图标启动时带的文件夹路径（macOS 的 -psn_xxx 参数要跳过）
    start_folder = folder_arg(args)
    if start_folder:
        window.new_vault(start_folder)

    # macOS：拖到图标 / open -a 传文件夹、Windows 的 QFileOpenEvent 都走这里
    folder_filter = FolderOpenFilter(window)
    app.installEventFilter(folder_filter)
    window._folder_filter = folder_filter  # 保持引用，防止被回收
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
