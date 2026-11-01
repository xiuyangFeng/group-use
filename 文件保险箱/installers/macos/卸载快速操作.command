#!/bin/zsh
# 双击运行：移除访达右键菜单里的「用文件保险箱上锁」。
set -e
WF="用文件保险箱上锁.workflow"
QUIET="${FDVAULT_INSTALL_QUIET:-0}"
TARGET="$HOME/Library/Services/$WF"

if [ -d "$TARGET" ]; then
  rm -rf "$TARGET"
  echo "已卸载：$TARGET"
else
  echo "没有找到已安装的快速操作（可能本来就没装）：$TARGET"
fi

if [ "${FDVAULT_SKIP_FINDER_RESTART:-0}" != "1" ]; then
  /usr/bin/killall Finder 2>/dev/null || true
fi
if [ "$QUIET" != "1" ]; then read -r "?按回车关闭"; fi
