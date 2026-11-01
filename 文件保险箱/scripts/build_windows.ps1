#Requires -Version 5.1
<#
.SYNOPSIS
  文件保险箱 Windows 一键构建脚本（在 Windows 10/11 上运行）。
.DESCRIPTION
  创建 .venv、安装依赖、调用 scripts/build_app.py 生成 dist\文件保险箱-…-Windows-x64.zip，
  并打印 SHA256SUMS.txt。需要 Python 3.11+ 和能上网装依赖。
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -SkipSmoke
#>
param(
    [switch]$NoClean,
    [switch]$SkipSmoke,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
Write-Host "工作目录：$(Get-Location)"

Write-Host "== 1/4 检查 Python =="
& $Python -c "import sys; assert sys.version_info >= (3, 11), '需要 Python 3.11 及以上，当前 ' + sys.version; print('Python', sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) { throw "找不到可用的 Python（可加 -Python py）" }

Write-Host "== 2/4 创建/复用虚拟环境 =="
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "创建 .venv 失败" }
}
& $venvPy -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "升级 pip 失败（检查网络）" }

Write-Host "== 3/4 安装依赖（含 PyInstaller） =="
& $venvPy -m pip install -r requirements.txt -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw "安装依赖失败（检查网络/代理）" }

Write-Host "== 4/4 打包 =="
$buildArgs = @("scripts/build_app.py")
if ($NoClean) { $buildArgs += "--no-clean" }
if ($SkipSmoke) { $buildArgs += "--skip-smoke" }
& $venvPy @buildArgs
if ($LASTEXITCODE -ne 0) { throw "构建失败（exit $LASTEXITCODE）" }

Write-Host ""
Write-Host "构建完成："
Get-ChildItem dist -Filter *.zip | ForEach-Object { Write-Host ("  " + $_.Name + "  " + [math]::Round($_.Length / 1MB, 1) + " MB") }
$sums = Join-Path $root "dist\SHA256SUMS.txt"
if (Test-Path $sums) {
    Write-Host ""
    Write-Host "SHA256："
    Get-Content $sums
}
Write-Host ""
Write-Host "下一步：解压 dist\*-Windows-x64.zip，双击 文件保险箱.exe，运行 installers\windows\验收自检.ps1"
