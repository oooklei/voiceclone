# 完整构建脚本（Rust + NSIS 安装包）
# 适用于发布

$ErrorActionPreference = "Stop"

Write-Host "=== 完整构建（Rust + NSIS） ===" -ForegroundColor Cyan
cd "$PSScriptRoot\src-tauri"
cargo tauri build 2>&1

if ($LASTEXITCODE -eq 0) {
    Write-Host "`n=== 构建完成 ===" -ForegroundColor Green
    Write-Host "安装包: $PSScriptRoot\src-tauri\target\release\bundle\nsis\" -ForegroundColor Yellow
} else {
    Write-Host "`n=== 构建失败 ===" -ForegroundColor Red
}
