# TIA Portal V21 MCP + 桌面控制台 两步打包脚本
#
# 第一步：Cython 编译 api/ core/ → PyInstaller onedir → realease_mcp\tia-portal-v21-mcp\
# 第二步：electron-builder 生成控制台 → 与 MCP 服务合并 → realease\TIA MCP 控制台\
#
# 用法：
#   .\build-release.ps1                 # 完整两步
#   .\build-release.ps1 -DesktopOnly    # 只重打控制台（复用 realease_mcp）
#
# 产物：realease\TIA MCP 控制台\（可直接复制到其它 Win10/Win11 x64 电脑运行）

[CmdletBinding()]
param(
    [switch]$DesktopOnly
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Desktop = Join-Path $Root "desktop"
$ReleaseMcp = Join-Path $Root "realease_mcp"
$ReleaseRoot = Join-Path $Root "realease"
$ProductName = "TIA MCP 控制台"
$Target = Join-Path $ReleaseRoot $ProductName
$McpDir = Join-Path $Target "mcp"

function Write-Section([string]$Text) {
    Write-Host ""
    Write-Host "== $Text ==" -ForegroundColor Cyan
}

function Remove-IfExists([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        Write-Host "清理 $Path"
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

if (-not (Test-Path $Python)) {
    throw "未找到虚拟环境解释器：$Python"
}

$Npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue)
if (-not $Npm) {
    throw "未找到 npm.cmd，请先安装 Node.js。"
}

$Stopwatch = [Diagnostics.Stopwatch]::StartNew()

# ---------------------------------------------------------------- 第一步
if (-not $DesktopOnly) {
    Write-Section "清理上次产物"
    Remove-IfExists $ReleaseMcp
    # 保留正在使用的便携目录，直到新后端与新桌面端都构建成功。
    # 这样构建中途失败不会提前删除用户日志和控制台设置。
    Remove-IfExists (Join-Path $Root "dist")
    Remove-IfExists (Join-Path $Root "build\cython")
    Remove-IfExists (Join-Path $Root "build\cython-temp")
    Remove-IfExists (Join-Path $Root "build\pyinstaller")

    Push-Location $Root
    try {
        & $Python -c "import Cython, PyInstaller" 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "缺少构建依赖。请执行：.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt"
        }

        Write-Section "Cython 编译（api / core）"
        & $Python "build\cython_build.py"
        if ($LASTEXITCODE -ne 0) { throw "Cython 编译失败。" }

        Write-Section "PyInstaller onedir"
        & $Python -m PyInstaller --noconfirm --clean `
            --distpath $ReleaseMcp `
            --workpath (Join-Path $Root "build\pyinstaller") `
            "build\tia_portal_v21_mcp.spec"
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败。" }

        $McpExe = Join-Path $ReleaseMcp "tia-portal-v21-mcp\tia-portal-v21-mcp.exe"
        if (-not (Test-Path $McpExe)) { throw "未找到 MCP 服务产物：$McpExe" }
        Write-Host "MCP 服务：$McpExe" -ForegroundColor Green

        Write-Section "清理 Python 中间产物"
        Remove-IfExists (Join-Path $Root "build\cython")
        Remove-IfExists (Join-Path $Root "build\cython-temp")
        Remove-IfExists (Join-Path $Root "build\pyinstaller")
        Remove-IfExists (Join-Path $Root "dist")
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-Path (Join-Path $ReleaseMcp "tia-portal-v21-mcp\tia-portal-v21-mcp.exe"))) {
    throw "缺少第一步产物 realease_mcp\tia-portal-v21-mcp，请先执行完整打包（去掉 -DesktopOnly）。"
}

# ---------------------------------------------------------------- 第二步
Write-Section "生成图标与工具元数据"
Push-Location $Root
try {
    & $Python "build\make_icons.py"
    if ($LASTEXITCODE -ne 0) { throw "图标生成失败。" }

    $InfoFile = Join-Path $Desktop "resources\mcp-info.json"
    New-Item -ItemType Directory -Force (Split-Path $InfoFile) | Out-Null
    $Info = & $Python "mcp_run.py" --info
    if ($LASTEXITCODE -ne 0) { throw "读取工具数量失败。" }
    # 必须写成无 BOM 的 UTF-8：Windows PowerShell 5.1 的 Set-Content -Encoding utf8 会带 BOM，
    # 而控制台用 JSON.parse 直接读这个文件，带 BOM 会解析失败（版本号/工具数丢失）。
    [IO.File]::WriteAllText($InfoFile, ($Info -join "`n"), (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "工具元数据：$InfoFile"
}
finally {
    Pop-Location
}

Write-Section "electron-builder 打包控制台"
Push-Location $Desktop
try {
    if (-not (Test-Path (Join-Path $Desktop "node_modules\electron"))) {
        Write-Host "安装前端依赖（首次较慢）…"
        $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
        & $Npm install --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw "npm install 失败。" }
    }

    $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
    $Builder = Join-Path $Desktop "node_modules\.bin\electron-builder.cmd"
    if (-not (Test-Path $Builder)) { throw "未找到 electron-builder，请先在 desktop 目录执行 npm install。" }
    & $Builder --win --x64 --dir
    if ($LASTEXITCODE -ne 0) { throw "electron-builder 打包失败。" }
}
finally {
    Pop-Location
}

$Unpacked = Join-Path $Desktop "dist\win-unpacked"
if (-not (Test-Path $Unpacked)) { throw "未找到 electron-builder 产物：$Unpacked" }

Write-Section "合并为便携目录"
$RuntimeBackup = $null
if (Test-Path -LiteralPath $Target) {
    $RuntimeBackup = Join-Path ([IO.Path]::GetTempPath()) ("tia-mcp-runtime-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force $RuntimeBackup | Out-Null
    foreach ($RuntimeName in @("log", "console-data")) {
        $RuntimePath = Join-Path $Target $RuntimeName
        if (Test-Path -LiteralPath $RuntimePath) {
            Copy-Item -LiteralPath $RuntimePath -Destination $RuntimeBackup -Recurse -Force
        }
    }
}
if (Test-Path -LiteralPath $Target) {
    # Chromium 的 console-data 缓存即使窗口刚关闭也可能仍被后台进程短暂占用。
    # 升级只替换程序文件，运行数据目录原地保留，避免因锁文件导致整个发布失败。
    Get-ChildItem -LiteralPath $Target -Force |
        Where-Object { $_.Name -notin @("log", "console-data") } |
        ForEach-Object { Remove-IfExists $_.FullName }
}
else {
    New-Item -ItemType Directory -Force $Target | Out-Null
}
Copy-Item -Path (Join-Path $Unpacked "*") -Destination $Target -Recurse -Force

New-Item -ItemType Directory -Force $McpDir | Out-Null
Copy-Item -Path (Join-Path $ReleaseMcp "tia-portal-v21-mcp\*") -Destination $McpDir -Recurse -Force

New-Item -ItemType Directory -Force (Join-Path $Target "log") | Out-Null
if ($RuntimeBackup -and (Test-Path -LiteralPath $RuntimeBackup)) {
    foreach ($RuntimeName in @("log", "console-data")) {
        $RuntimePath = Join-Path $RuntimeBackup $RuntimeName
        if (Test-Path -LiteralPath $RuntimePath) {
            Copy-Item -LiteralPath $RuntimePath -Destination $Target -Recurse -Force
        }
    }
    Remove-IfExists $RuntimeBackup
}

Write-Section "生成启动器"
# 部分电脑上 Chromium 沙箱初始化会失败，双击（无 --no-sandbox）时进程在加载 main.js 前就退出。
# 这里把 Electron 本体改名为 tia-mcp-console.exe，再用同名 exe 做启动器，
# 由启动器设置 ELECTRON_DISABLE_SANDBOX 并附带 --no-sandbox 启动本体。
$ProductExe = Join-Path $Target "$ProductName.exe"
$HostExe = Join-Path $Target "tia-mcp-console.exe"
if (-not (Test-Path -LiteralPath $ProductExe)) { throw "未找到 Electron 主程序：$ProductExe" }
Move-Item -LiteralPath $ProductExe -Destination $HostExe -Force

$LauncherSource = Join-Path $Root "build\launcher\Launcher.cs"
if (-not (Test-Path -LiteralPath $LauncherSource)) { throw "缺少启动器源码：$LauncherSource" }
$CscCandidates = @(
    (Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
    (Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe")
)
$Csc = $CscCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $Csc) { throw "未找到 C# 编译器 csc.exe（需要 .NET Framework 4.x），无法生成启动器。" }

& $Csc /nologo /target:winexe /codepage:65001 `
    "/win32icon:$(Join-Path $Root 'build\assets\app-icon.ico')" `
    "/out:$ProductExe" `
    $LauncherSource
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $ProductExe)) { throw "启动器编译失败。" }

Write-Section "校正文件完整性级别"
# 被沙箱/安全软件标记过的目录会把 Low 完整性级别继承给所有子文件，而 .NET Framework 会
# 拒绝从 Low（不受信任）位置加载程序集，报 0x80131515 并让服务在调用 TIA 工具时直接退出。
# 这里把发布目录统一提升为 Medium；失败时打印可以手工执行的命令。
$IntegrityProbe = Join-Path $McpDir "_internal\Siemens.TiaPortal.OpennessContracts.dll"
function Test-LowIntegrity([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $Acl = (& icacls $Path) -join "`n"
    return $Acl -match "Low Mandatory Level"
}
if ((Test-LowIntegrity $Target) -or (Test-LowIntegrity $IntegrityProbe)) {
    Write-Host "检测到 Low 完整性级别，正在提升为 Medium…"
    & icacls $Target /grant "$($env:USERNAME):(OI)(CI)F" /T /C 2>&1 | Out-Null
    & icacls $Target /setintegritylevel "(OI)(CI)Medium" /T /C 2>&1 | Out-Null
    if (Test-LowIntegrity $IntegrityProbe) {
        Write-Host ""
        Write-Host "警告：仍有文件停留在 Low 完整性级别，服务调用 TIA 工具时可能以 0xE0434352 退出。" -ForegroundColor Yellow
        Write-Host "请在一个普通的 PowerShell 窗口里执行：" -ForegroundColor Yellow
        Write-Host "  icacls `"$Target`" /grant `"$($env:USERNAME):(OI)(CI)F`" /T /C"
        Write-Host "  icacls `"$Target`" /setintegritylevel `"(OI)(CI)Medium`" /T /C"
        Write-Host "或者把整个「$Target」目录复制到其它位置（如 D:\TIA MCP 控制台）后运行。" -ForegroundColor Yellow
        Write-Host ""
    }
    else {
        Write-Host "已提升为 Medium 完整性级别。" -ForegroundColor Green
    }
}
else {
    Write-Host "完整性级别正常（Medium）。" -ForegroundColor Green
}

Write-Section "清理中间产物"
Remove-IfExists (Join-Path $Desktop "dist")
Remove-IfExists (Join-Path $Root "build\cython")
Remove-IfExists (Join-Path $Root "build\cython-temp")
Remove-IfExists (Join-Path $Root "build\pyinstaller")

# ---------------------------------------------------------------- 结果
$ConsoleExe = Join-Path $Target "$ProductName.exe"
$McpExePath = Join-Path $McpDir "tia-portal-v21-mcp.exe"
$TotalBytes = (Get-ChildItem -LiteralPath $Target -Recurse -File | Measure-Object Length -Sum).Sum

Write-Host ""
Write-Host "打包完成（用时 $([int]$Stopwatch.Elapsed.TotalSeconds) 秒）" -ForegroundColor Green
Write-Host "控制台：$ConsoleExe"
Write-Host "MCP 服务：$McpExePath"
Write-Host "目录大小：$([math]::Round($TotalBytes / 1MB, 1)) MB"
Write-Host ""
Write-Host "分发方式：整个「realease\TIA MCP 控制台」目录复制到目标电脑即可。"
Write-Host "首次启动 MCP 服务会弹出 Openness 授权，请确认程序路径后选择「是，全部接受」。"
Write-Host "授权与程序路径绑定，移动目录后需要重新授权。"

exit 0
