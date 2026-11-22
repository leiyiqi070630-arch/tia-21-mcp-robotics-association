# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：TIA Portal V21 MCP（2.2.1）。

必须使用 **onedir**（COLLECT），不要改成 onefile：
Openness 授权与 EXE 的路径/哈希绑定，onefile 每次启动都会解压到新的临时
目录，导致每次连接都重新弹出授权窗口。onedir 的 EXE 路径固定，授权可复用。

先在项目根目录执行 Cython staging：
    .venv\\Scripts\\python.exe build\\cython_build.py
再执行：
    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean build\\tia_portal_v21_mcp.spec
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

PROJECT_ROOT = Path(SPECPATH).resolve().parent
STAGE = PROJECT_ROOT / "build" / "cython"

if not (STAGE / "mcp_run.py").is_file():
    raise SystemExit(
        "缺少 Cython staging：请先运行 build\\cython_build.py 生成 build\\cython。"
    )

# siemens_tia_scripting 是一个 .pyd 加一组**同级** DLL：collect_all 只认得
# 包内数据，抓不到这些散落的 Openness 程序集。缺了它们，任何走 .NET 的工具
#（detect_tia_installations 等）都会抛 FileNotFoundException，进程以
# 0xE0434352 直接退出。这里显式把它们收进 _internal。
import importlib.util as _importlib_util

_module_spec = _importlib_util.find_spec("siemens_tia_scripting")
if _module_spec is None or not _module_spec.origin:
    raise SystemExit(
        "未找到 siemens_tia_scripting：请先在本机 Python 环境安装打包依赖。"
    )
SCRIPTING_DIR = Path(_module_spec.origin).resolve().parent
OPENNESS_DLLS = sorted(SCRIPTING_DIR.glob("Siemens.TiaPortal.*.dll"))
SCRIPTING_DEPENDENCIES = OPENNESS_DLLS + [SCRIPTING_DIR / "log4net.dll"]
missing_dependencies = [path.name for path in SCRIPTING_DEPENDENCIES if not path.is_file()]
if missing_dependencies:
    raise SystemExit(
        f"{SCRIPTING_DIR} 缺少 TIA Scripting 依赖：{missing_dependencies}。"
        "打出来的服务可能无法 attach 或会以 0xE0434352 崩溃。"
    )

# 运行时只允许 V21；保留其它 DLL 是为了让桥接库自身能正常加载/枚举。
datas, binaries, hiddenimports = collect_all("siemens_tia_scripting")
datas = list(datas)
binaries = list(binaries) + [(str(path), ".") for path in SCRIPTING_DEPENDENCIES]
hiddenimports = list(hiddenimports)

# Cython 编译产物是扩展模块，PyInstaller 不会从入口自动推导出全部模块名。
for package in ("api", "core"):
    for extension in sorted((STAGE / package).glob("*.pyd")):
        hiddenimports.append(f"{package}.{extension.name.split('.')[0]}")

analysis = Analysis(
    [str(STAGE / "mcp_run.py")],
    pathex=[str(STAGE)],
    binaries=binaries,
    datas=datas
    + [
        (str(STAGE / "device_templates"), "device_templates"),
        (str(STAGE / "app-icon.ico"), "."),
        (str(PROJECT_ROOT / "LICENSE"), "."),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "_pytest",
        "Cython",
        "setuptools",
        "pip",
        "wheel",
        "tkinter",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="tia-portal-v21-mcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(STAGE / "app-icon.ico"),
)

COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="tia-portal-v21-mcp",
)
