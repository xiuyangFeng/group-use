"""macOS 快速操作（Finder 右键）结构检查 + 脚本逻辑检查。

真正注册到 Finder 需要在目标 Mac 上双击 安装快速操作.command；
这里验证 .workflow 结构和脚本参数处理，避免发出去的包是坏的。
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
WF = PROJECT / "installers" / "macos" / "用文件保险箱上锁.workflow"


@pytest.mark.skipif(os.name != "posix" or os.uname().sysname != "Darwin", reason="仅 macOS")
def test_workflow_structure():
    assert (WF / "Contents" / "document.wflow").exists()
    assert (WF / "Contents" / "Info.plist").exists()
    info = plistlib.loads((WF / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"]
    service = info["NSServices"][0]
    assert service["NSMessage"] == "runWorkflowAsService"
    assert service["NSMenuItem"]["default"] == "用文件保险箱上锁"
    assert "public.folder" in service["NSSendFileTypes"]
    assert service["NSRequiredContext"]["NSApplicationIdentifier"] == "com.apple.finder"

    wf = plistlib.loads((WF / "Contents" / "document.wflow").read_bytes())
    actions = [a["action"] for a in wf["actions"]]
    shell_actions = [a for a in actions if a.get("BundleIdentifier") == "com.apple.RunShellScript"]
    assert shell_actions, "缺少 Run Shell Script"
    script = shell_actions[0]["ActionParameters"]["COMMAND_STRING"]
    assert "文件保险箱" in script
    assert "open" in script
    assert "-d" in script, "要先判断是不是文件夹"


@pytest.mark.skipif(os.name != "posix" or os.uname().sysname != "Darwin", reason="仅 macOS")
def test_quick_action_installer_installs_into_fake_home(tmp_path):
    """在临时 HOME 里跑安装/卸载脚本，验证快速操作真的被放到 ~/Library/Services。"""
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(tmp_path),
            "FDVAULT_INSTALL_QUIET": "1",
            "FDVAULT_SKIP_FINDER_RESTART": "1",
        }
    )
    install = PROJECT / "installers" / "macos" / "安装快速操作.command"
    proc = subprocess.run(["/bin/zsh", str(install)], env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    installed = tmp_path / "Library" / "Services" / "用文件保险箱上锁.workflow"
    assert (installed / "Contents" / "document.wflow").exists()
    assert (installed / "Contents" / "Info.plist").exists()
    info = plistlib.loads((installed / "Contents" / "Info.plist").read_bytes())
    assert info["NSServices"][0]["NSMenuItem"]["default"] == "用文件保险箱上锁"

    uninstall = PROJECT / "installers" / "macos" / "卸载快速操作.command"
    proc = subprocess.run(["/bin/zsh", str(uninstall)], env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert not installed.exists()


@pytest.mark.skipif(os.name != "posix" or os.uname().sysname != "Darwin", reason="仅 macOS")
def test_workflow_script_handles_folder(tmp_path):
    """把 /usr/bin/open 换成打印，验证脚本能正确取出文件夹参数。"""
    wf = plistlib.loads((WF / "Contents" / "document.wflow").read_bytes())
    script = next(
        a["action"]["ActionParameters"]["COMMAND_STRING"]
        for a in wf["actions"]
        if a["action"].get("BundleIdentifier") == "com.apple.RunShellScript"
    )
    probe = script.replace("/usr/bin/open", 'printf "OPEN_ARG=%s\\n"')
    target = tmp_path / "待上锁文件夹"
    target.mkdir()
    script_file = tmp_path / "probe.zsh"
    script_file.write_text(probe, encoding="utf-8")
    result = subprocess.run(
        ["/bin/zsh", str(script_file), str(target)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    open_args = [line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("OPEN_ARG=")]
    assert str(target) in open_args, (result.stdout, open_args)
    # 传入的不是文件夹（比如普通文件）时，不应该调用 open
    other = tmp_path / "a.txt"
    other.write_text("x", encoding="utf-8")
    result2 = subprocess.run(["/bin/zsh", str(script_file), str(other)], capture_output=True, text=True, timeout=30)
    assert "OPEN_ARG=" not in result2.stdout
