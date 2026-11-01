#!/bin/zsh
# 双击运行：把「用文件保险箱上锁」装到访达右键菜单（快速操作）。
# 不需要管理员权限；只影响当前用户。
# 卸载：双击 卸载快速操作.command，或删除 ~/Library/Services/用文件保险箱上锁.workflow
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
WF="用文件保险箱上锁.workflow"
QUIET="${FDVAULT_INSTALL_QUIET:-0}"

if [ ! -d "$DIR/$WF" ]; then
  echo "找不到 $DIR/$WF"
  echo "请确认这个脚本和 .workflow 在同一个文件夹里。"
  if [ "$QUIET" != "1" ]; then read -r "?按回车退出"; fi
  exit 1
fi

mkdir -p "$HOME/Library/Services"
rm -rf "$HOME/Library/Services/$WF"
cp -R "$DIR/$WF" "$HOME/Library/Services/$WF"

# 测试时用 FDVAULT_SKIP_FINDER_RESTART=1 跳过，避免影响正在用的访达
if [ "${FDVAULT_SKIP_FINDER_RESTART:-0}" != "1" ]; then
  /usr/bin/killall Finder 2>/dev/null || true
fi

echo ""
echo "已安装：右键任意文件夹 → 快速操作 → 用文件保险箱上锁"
echo "如果没看到，先重启访达（Finder 会自动重启），或者看 系统设置 → 隐私与安全性 → 扩展。"
echo "程序需要放在「应用程序」文件夹，或者名字能被右键菜单找到。"
echo ""
if [ "$QUIET" != "1" ]; then read -r "?按回车关闭"; fi
