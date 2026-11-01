"""图形界面启动入口（源码运行与 PyInstaller 打包共用）：python run_gui.py"""

from gui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
