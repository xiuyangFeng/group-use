# Windows 真机验收自检：双击右键"使用 PowerShell 运行"。
# 能自动验证的自动跑，不能自动的列出步骤；结果写到同目录 验收结果.txt。
$ErrorActionPreference = "Continue"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$report = Join-Path $scriptDir "验收结果.txt"
$lines = New-Object System.Collections.Generic.List[string]
function Add-Line([string]$text) {
    Write-Host $text
    $lines.Add($text)
}
function Add-Result([string]$name, [bool]$ok, [string]$detail = "") {
    $mark = if ($ok) { "通过" } else { "失败" }
    Add-Line ("[{0}] {1} {2}" -f $mark, $name, $detail)
}

Add-Line ("文件保险箱 Windows 验收自检 - " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Add-Line ("操作系统：" + [System.Environment]::OSVersion.VersionString)
Add-Line ""

# 1. 找程序
$exe = Join-Path $scriptDir "文件保险箱.exe"
if (-not (Test-Path $exe)) {
    $candidate = Get-ChildItem -Path $scriptDir -Filter "文件保险箱.exe" -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($candidate) { $exe = $candidate.FullName }
}
Add-Result "找到 文件保险箱.exe" (Test-Path $exe) $exe

# 2. 打包自检（加密核心 + 界面组件）
if (Test-Path $exe) {
    Write-Host "正在运行打包自检（约 10 秒）..."
    $env:QT_QPA_PLATFORM = "offscreen"
    $selfReport = Join-Path $env:TEMP "fdvault-selftest-report.txt"
    $output = & $exe --selftest --report $selfReport 2>&1 | Out-String
    $code = $LASTEXITCODE
    if (Test-Path $selfReport) { $output = Get-Content $selfReport -Raw }
    Add-Line $output.Trim()
    Add-Result "打包自检（--selftest）" (($code -eq 0) -and ($output -match "SELFCHECK PASS")) "exit=$code"

    Write-Host "正在运行真实参数验收（约 10 秒）..."
    $accReport = Join-Path $env:TEMP "fdvault-acceptance-report.txt"
    $output = & $exe --acceptance --report $accReport 2>&1 | Out-String
    $code = $LASTEXITCODE
    if (Test-Path $accReport) { $output = Get-Content $accReport -Raw }
    Add-Line $output.Trim()
    Add-Result "真实参数验收（--acceptance）" (($code -eq 0) -and ($output -match "ACCEPTANCE PASS")) "exit=$code"
    Remove-Item Env:\QT_QPA_PLATFORM -ErrorAction SilentlyContinue
}

# 3. 右键菜单是否已安装
$regPath = "HKCU:\Software\Classes\Directory\shell\文件保险箱"
$hasMenu = Test-Path $regPath
$cmd = if ($hasMenu) { (Get-ItemProperty -Path "$regPath\command" -ErrorAction SilentlyContinue)."(default)" } else { "" }
Add-Result "右键菜单已安装" $hasMenu $cmd

# 4. 数据目录可写
$dataDir = Join-Path $env:APPDATA "组内工具\文件保险箱"
try {
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    $probe = Join-Path $dataDir ".write-test"
    "ok" | Set-Content -Encoding UTF8 $probe
    Remove-Item $probe -Force
    Add-Result "数据目录可写" $true $dataDir
} catch {
    Add-Result "数据目录可写" $false $_.Exception.Message
}

# 5. 手工步骤清单
Add-Line ""
Add-Line "===== 以下需要手工点一遍 ====="
Add-Line "1. 双击 文件保险箱.exe，建一个测试保险箱（选一个小文件夹），记下恢复密钥。"
Add-Line "2. 解锁 → 用 Excel 打开里面的表格不关 → 点“立即上锁”，应提示文件被占用并中止。"
Add-Line "3. 关掉 Excel 再上锁，应成功；文件夹里只剩 .vault 和 如何解锁.txt。"
Add-Line "4. 退出程序再打开，看是否提示“上次没上锁”（如果刚才没锁）。"
Add-Line "5. 右键任意文件夹 → 用文件保险箱上锁，确认程序打开并预填该文件夹。"
Add-Line "6. 用中文/空格/长路径（如 D:\测试 数据\患者 名单）再建一个，确认能解锁。"
Add-Line "7. 建第二个 Windows 账户，登录后确认打不开另一个账户的保险箱文件夹。"
Add-Line ""
Add-Line "自动部分结果：" + (($lines | Where-Object { $_ -match "^\[失败\]" }).Count) + " 项失败"
$lines | Set-Content -Encoding UTF8 $report
Write-Host ""
Write-Host "已写入：$report" -ForegroundColor Green
Read-Host "按回车退出"
