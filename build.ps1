# VoiceClone 统一构建脚本
# 用法:
#   .\build.ps1           # 增量构建（只重建改动部分）
#   .\build.ps1 -Full     # 全量重建
#   .\build.ps1 -Fast     # 仅 Rust（跳过 NSIS，最快）
#   .\build.ps1 -Sidecar  # 仅重建 sidecar

param(
    [switch]$Full,
    [switch]$Fast,
    [switch]$SidecarOnly
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$SidecarDir = "$Root\dist"
$TauriDir = "$Root\voiceclone-desktop\src-tauri"
$StaticDir = "$Root\static"
$CacheFile = "$Root\.build_cache"

# ---------- 工具函数 ----------
function Get-FileHash($path) {
    if (-not (Test-Path $path)) { return "" }
    (Microsoft.PowerShell.Utility\Get-FileHash $path -Algorithm MD5).Hash
}

function Get-DirHash($dir) {
    if (-not (Test-Path $dir)) { return "" }
    $files = Get-ChildItem $dir -Recurse -File | Sort-Object FullName
    $hasher = [System.Security.Cryptography.MD5]::Create()
    foreach ($f in $files) {
        $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
        $hasher.TransformBlock($bytes, 0, $bytes.Length, $null, 0) | Out-Null
    }
    $hasher.TransformFinalBlock(@(), 0, 0) | Out-Null
    [BitConverter]::ToString($hasher.Hash).Replace("-", "")
}

function Read-Cache {
    if (Test-Path $CacheFile) {
        Get-Content $CacheFile | ConvertFrom-Json
    } else {
        @{ sidecar = ""; static = ""; rust = "" }
    }
}

function Write-Cache($cache) {
    $cache | ConvertTo-Json | Set-Content $CacheFile -Encoding UTF8
}

# ---------- 检测变更 ----------
Write-Host "=== 检测变更 ===" -ForegroundColor Cyan

$staticHash = Get-DirHash $StaticDir
$pyFiles = Get-ChildItem "$Root\app" -Recurse -File -Include *.py
$pyFiles += Get-ChildItem "$Root\sidecar.py", "$Root\packaging\sidecar.spec" -ErrorAction SilentlyContinue
$pyHash = ($pyFiles | ForEach-Object { Get-FileHash $_.FullName } | Sort-Object) -join ""

$cache = Read-Cache
$staticChanged = ($staticHash -ne $cache.static) -or $Full
$pyChanged = ($pyHash -ne $cache.sidecar) -or $Full

Write-Host "  static/ 变更: $staticChanged"
Write-Host "  Python 变更: $pyChanged"

# ---------- 构建 Sidecar ----------
if ($SidecarOnly -or $staticChanged -or $pyChanged -or $Full) {
    Write-Host "`n=== 重建 Sidecar (PyInstaller) ===" -ForegroundColor Cyan

    # 清理旧构建
    Remove-Item "$Root\build\sidecar" -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item "$Root\dist\sidecar" -Recurse -Force -ErrorAction SilentlyContinue

    # 打包
    & "$Root\.venv\Scripts\python.exe" -m PyInstaller "$Root\packaging\sidecar.spec" --noconfirm 2>&1 | Select-Object -Last 5

    if ($LASTEXITCODE -ne 0) { throw "Sidecar 构建失败" }

    # 复制到 Tauri resources（正确位置）
    $tauriResources = "$TauriDir\target\release\resources\sidecar"
    if (Test-Path $tauriResources) { Remove-Item $tauriResources -Recurse -Force -ErrorAction SilentlyContinue }
    Copy-Item "$SidecarDir\sidecar" $tauriResources -Recurse -Force

    # 也复制到 target/release/sidecar（兼容旧路径）
    if (Test-Path "$TauriDir\target\release\sidecar") {
        Remove-Item "$TauriDir\target\release\sidecar" -Recurse -Force -ErrorAction SilentlyContinue
    }
    Copy-Item "$SidecarDir\sidecar" "$TauriDir\target\release\sidecar" -Recurse -Force

    Write-Host "  Sidecar 构建完成 → resources/sidecar" -ForegroundColor Green

    # 更新缓存
    $cache.sidecar = $pyHash
    $cache.static = $staticHash
    Write-Cache $cache
} elseif (-not $Fast) {
    Write-Host "`n=== Sidecar 无跳过 ===" -ForegroundColor Yellow
}

if ($SidecarOnly) {
    Write-Host "`n=== 完成（仅 Sidecar） ===" -ForegroundColor Green
    exit 0
}

# ---------- 构建 Tauri ----------
if ($Fast) {
    Write-Host "`n=== 构建 Rust（跳过 NSIS） ===" -ForegroundColor Cyan
    cd $TauriDir
    cargo build --release 2>&1 | Select-Object -Last 3
    if ($LASTEXITCODE -ne 0) { throw "Rust 构建失败" }
    Write-Host "`n=== 完成 ===" -ForegroundColor Green
    Write-Host "exe: $TauriDir\target\release\voiceclone-desktop.exe" -ForegroundColor Yellow
} else {
    Write-Host "`n=== 构建 Tauri（Rust + NSIS） ===" -ForegroundColor Cyan
    cd $TauriDir
    cargo tauri build 2>&1 | Select-Object -Last 3
    if ($LASTEXITCODE -ne 0) { throw "Tauri 构建失败" }
    Write-Host "`n=== 完成 ===" -ForegroundColor Green
    Write-Host "安装包: $TauriDir\target\release\bundle\nsis\" -ForegroundColor Yellow
}
