# 快速构建脚本（仅 Rust，跳过 NSIS）
# 适用于前端/JS 修改后的快速测试

$ErrorActionPreference = "Stop"

Write-Host "=== 构建 Rust 后端 ===" -ForegroundColor Cyan
cd "$PSScriptRoot\src-tauri"
cargo build --release 2>&1

if ($LASTEXITCODE -eq 0) {
    Write-Host "`n=== 构建完成 ===" -ForegroundColor Green
    Write-Host "exe: $PSScriptRoot\src-tauri\target\release\voiceclone-desktop.exe" -ForegroundColor Yellow
    Write-Host "`n直接运行测试（无需 NSIS 安装包）：" -ForegroundColor Cyan
    Write-Host "  & '$PSScriptRoot\src-tauri\target\release\voiceclone-desktop.exe'" -ForegroundColor White
} else {
    Write-Host "`n=== 构建失败 ===" -ForegroundColor Red
}
