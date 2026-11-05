# Windows：移除“用文件保险箱上锁”右键菜单

# 右键本文件 →“使用 PowerShell 运行”，不需要管理员权限。

$regPath = "HKCU:\Software\Classes\Directory\shell\文件保险箱"
if (Test-Path $regPath) {
    Remove-Item -Path $regPath -Recurse -Force
    Write-Host "已移除右键菜单。" -ForegroundColor Green
} else {
    Write-Host "没有找到右键菜单（可能已经移除过）。"
}
Read-Host "按回车退出"
