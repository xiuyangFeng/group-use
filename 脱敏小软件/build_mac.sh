#!/bin/bash
# 在 Mac 上打包免安装的 CT_Deid.app（自带 Python 和所有依赖，对方不用装任何东西）。
# 需要 uv（https://docs.astral.sh/uv/）。用法：
#   ./build_mac.sh            # 打本机架构（Apple 芯片 → arm64）
#   ./build_mac.sh x86_64     # 在 Apple 芯片的 Mac 上通过 Rosetta 打 Intel 版
# 产物：release/CT_Deid_Mac_<架构>.zip
set -euo pipefail
cd "$(dirname "$0")"
ARCH="${1:-$(uname -m)}"
case "$ARCH" in
  arm64) PY="cpython-3.12-macos-aarch64-none"; RUN=(env); NAME="AppleSilicon" ;;
  x86_64) PY="cpython-3.12-macos-x86_64-none"; RUN=(arch -x86_64); NAME="Intel" ;;
  *) echo "不支持的架构：$ARCH"; exit 1 ;;
esac
VENV=".venv-build-$ARCH"
[ -x "$VENV/bin/python" ] || uv venv -q "$VENV" -p "$PY"
uv pip install -q -p "$VENV/bin/python" -r requirements.txt pyinstaller
"${RUN[@]}" "$VENV/bin/python" -m PyInstaller --noconfirm --clean --windowed --name CT_Deid \
  --osx-bundle-identifier edu.lab.ctdeid \
  --icon "$PWD/deid/icons/app.icns" --add-data "$PWD/deid/icons:deid/icons" \
  --distpath "build-mac/$ARCH/dist" --workpath "build-mac/$ARCH/work" --specpath "build-mac/$ARCH" app.py
APP="build-mac/$ARCH/dist/CT_Deid.app"
file "$APP/Contents/MacOS/CT_Deid"

# 打包前用假数据跑一遍打包后的程序（含多进程），确认能用
T=$(mktemp -d)
"${RUN[@]}" "$VENV/bin/python" tools/make_fake_data.py "$T/input" >/dev/null
DEID_HOME="$T/home" "${RUN[@]}" "$APP/Contents/MacOS/CT_Deid" \
  --input "$T/input" --output "$T/out" --mapping "$T/local/map.xlsx" --yes --workers 2 >/dev/null
N=$(find "$T/out" -name '*.dcm' | wc -l | tr -d ' ')
[ "$N" -ge 10 ] || { echo "[错误] 自检失败：只写出 $N 个文件"; exit 1; }
echo "自检通过：写出 $N 个文件"

# 单患者模式自检：直接选一个患者文件夹（平铺结构），应自动识别成一个患者
DEID_HOME="$T/home" "${RUN[@]}" "$APP/Contents/MacOS/CT_Deid" \
  --input "$T/input/WANG_WU_1" --output "$T/out_one" --mapping "$T/local/map_one.xlsx" --yes --workers 2 >/dev/null
N1=$(find "$T/out_one/P0001" -name '*.dcm' 2>/dev/null | wc -l | tr -d ' ')
[ "$N1" -ge 1 ] && [ ! -d "$T/out_one/P0002" ] \
  || { echo "[错误] 单患者模式自检失败：P0001 写出 $N1 个文件"; exit 1; }
echo "单患者模式自检通过：写出 $N1 个文件"

mkdir -p release
STAGE=$(mktemp -d)/CT_Deid_Mac_$NAME
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
cp 使用说明.txt "$STAGE/"
rm -f "release/CT_Deid_Mac_$NAME.zip"
ditto -c -k --norsrc --keepParent "$STAGE" "release/CT_Deid_Mac_$NAME.zip"   # ditto 保留签名和符号链接
echo "完成：release/CT_Deid_Mac_$NAME.zip"
