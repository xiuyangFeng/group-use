# Windows：给文件夹加“用文件保险箱上锁”右键菜单
#
# 用法：把 文件保险箱 文件夹放到固定位置（比如 D:\工具\文件保险箱），
# 然后右键本文件 →“使用 PowerShell 运行”。
# 只想给当前用户加菜单，不需要管理员权限。

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe = Join-Path $scriptDir "文件保险箱.exe"

if (-not (Test-Path $exe)) {
    # 也支持“安装包解压后和这个脚本不在同一层”的情况
    $candidate = Get-ChildItem -Path $scriptDir -Filter "文件保险箱.exe" -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($candidate) { $exe = $candidate.FullName }
}

if (-not (Test-Path $exe)) {
    Write-Host "找不到 文件保险箱.exe。请把本脚本放到解压后的程序文件夹里再运行。" -ForegroundColor Red
    Write-Host "（当前目录：$scriptDir）"
    Read-Host "按回车退出"
    exit 1
}

$regPath = "HKCU:\Software\Classes\Directory\shell\文件保险箱"

New-Item -Path $regPath -Force | Out-Null
Set-ItemProperty -Path $regPath -Name "(Default)" -Value "用文件保险箱上锁"
Set-ItemProperty -Path $regPath -Name "Icon" -Value "`"$exe`""
New-Item -Path "$regPath\command" -Force | Out-Null
Set-ItemProperty -Path "$regPath\command" -Name "(Default)" -Value "`"$exe`" `"%1`""

Write-Host "已添加右键菜单：" -ForegroundColor Green
Write-Host "  在资源管理器里右键任意文件夹 → 显示更多选项（Win11）/ 直接看（Win10）→ 用文件保险箱上锁"
Write-Host "  程序位置：$exe"
Read-Host "按回车退出"
