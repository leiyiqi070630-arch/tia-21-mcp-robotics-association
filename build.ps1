# TIA Portal V21 MCP 打包脚本
#
# 只做两件事：
#   1. build\cython_build.py 把 api/ 与 core/ 编译成 .pyd（staging 到 build\cython）
#   2. PyInstaller 用 build\tia_portal_v21_mcp.spec 生成 onedir 目录
#
# 本脚本不会自动安装依赖，也不会自动运行；需要时手动执行：
#   .\build.ps1
#   .\build.ps1 -SkipCython      # 复用已有 build\cython
#
# 产物：dist\tia-portal-v21-mcp\tia-portal-v21-mcp.exe

[CmdletBinding()]
param(
    [switch]$SkipCython
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "未找到虚拟环境解释器：$Python。请先在本工作区创建 .venv。"
}

Push-Location $Root
try {
    Write-Host "== Python ==" -ForegroundColor Cyan
    & $Python -c "import sys; print(sys.version)"

    & $Python -c "import Cython, PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "缺少构建依赖。请执行：.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt"
    }

    if (-not $SkipCython) {
        Write-Host "== Cython 编译 ==" -ForegroundColor Cyan
        & $Python "build\cython_build.py"
        if ($LASTEXITCODE -ne 0) {
            throw "Cython 编译失败，已停止打包。"
        }
    }

    Write-Host "== PyInstaller（onedir） ==" -ForegroundColor Cyan
    & $Python -m PyInstaller --noconfirm --clean "build\tia_portal_v21_mcp.spec"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 打包失败。"
    }

    $Exe = Join-Path $Root "dist\tia-portal-v21-mcp\tia-portal-v21-mcp.exe"
    if (-not (Test-Path $Exe)) {
        throw "打包完成但未找到产物：$Exe"
    }
    Write-Host ""
    Write-Host "打包完成：$Exe" -ForegroundColor Green
    Write-Host "首次运行会弹出 Openness 授权，请确认程序路径后选择「是，全部接受」。"
    Write-Host "不要把 dist 目录移到新路径后继续用旧授权；路径变化会重新弹窗。"
}
finally {
    Pop-Location
}
