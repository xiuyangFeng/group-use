#!/bin/bash
# Mac 双击运行（和 Windows 的 run_from_source.bat 一样）。第一次运行会自动安装依赖到本文件夹的 .venv-run 里。
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3)
if [ -z "$PY" ]; then
  echo "[错误] 没找到 python3。请从 python.org 安装 Python 3.12，再双击本文件。"
  read -r -p "按回车关闭"; exit 1
fi
if [ ! -x .venv-run/bin/python ]; then
  echo "第一次运行，正在安装依赖（只需要一次）……"
  "$PY" -m venv .venv-run \
    && { .venv-run/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt \
         || .venv-run/bin/python -m pip install -q --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt; } \
    || { echo "[错误] 安装失败，请把这个窗口截图发给开发者。"; read -r -p "按回车关闭"; exit 1; }
fi
.venv-run/bin/python -c "import tkinter" 2>/dev/null \
  || { echo "[错误] 这个 Python 没有图形界面组件（tkinter）。请从 python.org 安装 Python 3.12 后再试。"; read -r -p "按回车关闭"; exit 1; }
exec .venv-run/bin/python app.py "$@"
