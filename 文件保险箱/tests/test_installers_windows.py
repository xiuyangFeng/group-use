"""Windows 安装脚本静态检查（真机验证之前的兜底）。

PowerShell 会被 CI 的 Windows runner 解析；本机没有 PowerShell 就只做文本断言。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
WIN = PROJECT / "installers" / "windows"


def test_install_script_registers_user_context_menu():
    text = (WIN / "安装右键菜单.ps1").read_text(encoding="utf-8")
    assert "HKCU:\\Software\\Classes\\Directory\\shell" in text
    assert "用文件保险箱上锁" in text
    assert "%1" in text
    assert "Icon" in text
    # 只写当前用户，不能要求管理员/写 HKLM
    assert "HKEY_LOCAL_MACHINE" not in text
    assert "RunAs" not in text


def test_uninstall_script_removes_same_key():
    text = (WIN / "卸载右键菜单.ps1").read_text(encoding="utf-8")
    assert "HKCU:\\Software\\Classes\\Directory\\shell" in text
    assert "Remove-Item" in text
    assert "文件保险箱" in text


def test_windows_install_guide_mentions_smartscreen_and_menu():
    text = (WIN / "安装说明.md").read_text(encoding="utf-8")
    assert "SmartScreen" in text
    assert "更多信息" in text
    assert "右键" in text
    assert "文件保险箱.exe" in text


def _find_powershell() -> str | None:
    for name in ("pwsh", "powershell"):
        exe = shutil.which(name)
        if exe:
            return exe
    return None


@pytest.mark.skipif(_find_powershell() is None, reason="本机没有 PowerShell（Windows CI 会跑）")
@pytest.mark.parametrize("script", ["安装右键菜单.ps1", "卸载右键菜单.ps1"])
def test_powershell_syntax(script: str):
    exe = _find_powershell()
    ps = (WIN / script).as_posix()
    code = (
        "$tokens=$null; $errors=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{ps}', [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
    )
    proc = subprocess.run([exe, "-NoProfile", "-Command", code], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


def test_acceptance_script_checks_packaged_selftest_and_menu():
    script = WIN / "验收自检.ps1"
    assert script.exists()
    text = script.read_text(encoding="utf-8")
    assert "--selftest" in text
    assert "SELFCHECK PASS" in text
    assert "--acceptance" in text
    assert "ACCEPTANCE PASS" in text
    assert "HKCU:\\Software\\Classes\\Directory\\shell" in text
    assert "验收结果.txt" in text
    assert "Excel" in text  # 手工步骤里要测文件占用
    assert "第二个 Windows 账户" in text


@pytest.mark.skipif(_find_powershell() is None, reason="本机没有 PowerShell（Windows CI 会跑）")
def test_acceptance_script_powershell_syntax():
    exe = _find_powershell()
    ps = (WIN / "验收自检.ps1").as_posix()
    code = (
        "$tokens=$null; $errors=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{ps}', [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
    )
    proc = subprocess.run([exe, "-NoProfile", "-Command", code], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


def test_windows_build_script():
    """Windows 一键构建脚本：给真机/CI 用，命令必须齐。"""
    script = PROJECT / "scripts" / "build_windows.ps1"
    assert script.exists()
    text = script.read_text(encoding="utf-8-sig")
    assert text.startswith("#Requires -Version 5.1")
    assert "build_app.py" in text
    assert ".venv\\Scripts\\python.exe" in text
    assert "requirements.txt" in text and "requirements-dev.txt" in text
    assert "SHA256SUMS.txt" in text
    assert "$SkipSmoke" in text and "$NoClean" in text


def test_powershell_scripts_have_utf8_bom():
    """Windows 自带的 PowerShell 5.1 把没有 BOM 的脚本当成系统 ANSI（中文系统是 GBK）读，
    脚本里的中文（包括「文件保险箱.exe」）会变乱码甚至语法错误。必须带 UTF-8 BOM。"""
    scripts = list(WIN.glob("*.ps1")) + list((PROJECT / "scripts").glob("*.ps1"))
    assert scripts
    for script in scripts:
        assert script.read_bytes().startswith(b"\xef\xbb\xbf"), f"{script.name} 缺 UTF-8 BOM"


def test_windows_zip_extras_exist():
    """Windows 压缩包里和程序放在一起的说明 / 脚本都要存在（见 scripts/build_app.py）。"""
    import sys

    sys.path.insert(0, str(PROJECT / "scripts"))
    import build_app

    for name in build_app.WINDOWS_EXTRAS:
        assert (WIN / name).is_file(), name
    text = build_app.windows_readme("9.9.9")
    assert "不需要安装 Python" in text and "文件保险箱.exe" in text
