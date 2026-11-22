"""用 Cython 把 core/ 与 api/ 编译成扩展模块，生成 PyInstaller 的 staging 目录。

设计要点
--------
* 只编译业务包 ``core`` 与 ``api``；``mcp_run.py`` 保留为普通 Python 入口，
  这样 PyInstaller 有明确的脚本入口，也便于排查启动问题。
* 输出到 ``build/cython/``，不污染源码目录；源码里的 ``.py`` 不会被替换。
* 编译后的扩展模块与 ``__init__.py``、模板资源放在同一 staging 树中，
  PyInstaller 直接以该目录为 ``pathex`` 打包。
* 本脚本只负责编译，不调用 PyInstaller；打包由 ``build.ps1`` 或手动执行
  ``python -m PyInstaller build\\tia_portal_v21_mcp.spec`` 完成。

用法::

    .venv\\Scripts\\python.exe build\\cython_build.py
    .venv\\Scripts\\python.exe build\\cython_build.py --keep-temp
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STAGE = PROJECT_ROOT / "build" / "cython"
TEMP = PROJECT_ROOT / "build" / "cython-temp"
PACKAGES = ("api", "core")
COPY_FILES = ("mcp_run.py",)
COPY_ASSETS = ((PROJECT_ROOT / "build" / "assets" / "app-icon.ico", "app-icon.ico"),)
COPY_DIRS = ("device_templates",)


def _reset_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _stage_sources() -> list[Path]:
    """复制入口、包标记与资源，返回需要编译的 .py 列表。"""
    _reset_directory(STAGE)
    compiled: list[Path] = []
    for name in COPY_FILES:
        source = PROJECT_ROOT / name
        if not source.is_file():
            raise FileNotFoundError(f"缺少打包资源：{source}")
        shutil.copy2(source, STAGE / name)
    for source, target_name in COPY_ASSETS:
        if not source.is_file():
            raise FileNotFoundError(f"缺少打包资源：{source}")
        shutil.copy2(source, STAGE / target_name)
    for name in COPY_DIRS:
        source = PROJECT_ROOT / name
        if not source.is_dir():
            raise FileNotFoundError(f"缺少打包资源目录：{source}")
        shutil.copytree(source, STAGE / name)

    for package in PACKAGES:
        source_dir = PROJECT_ROOT / package
        if not source_dir.is_dir():
            raise FileNotFoundError(f"缺少源码包：{source_dir}")
        target_dir = STAGE / package
        target_dir.mkdir(parents=True, exist_ok=True)
        init_file = source_dir / "__init__.py"
        if init_file.is_file():
            shutil.copy2(init_file, target_dir / "__init__.py")
        for module in sorted(source_dir.glob("*.py")):
            if module.name == "__init__.py":
                continue
            compiled.append(module)
    return compiled


def _build_extensions(modules: list[Path]) -> None:
    from Cython.Build import cythonize
    from setuptools import Extension
    from setuptools.dist import Distribution

    extensions = [
        Extension(
            name=f"{module.parent.name}.{module.stem}",
            sources=[str(module)],
        )
        for module in modules
    ]
    TEMP.mkdir(parents=True, exist_ok=True)
    # 生成的 C 文件也放进 build/，避免在 api/、core/ 里留下 .c。
    cython_c = TEMP / "c"
    cython_c.mkdir(parents=True, exist_ok=True)
    cythonized = cythonize(
        extensions,
        build_dir=str(cython_c),
        language_level="3",
        compiler_directives={
            "language_level": "3",
            "binding": True,
            "embedsignature": True,
            "always_allow_keywords": True,
            "initializedcheck": False,
            "nonecheck": False,
        },
        quiet=False,
    )
    distribution = Distribution(
        {
            "name": "tia-portal-v21-mcp-cython",
            "ext_modules": cythonized,
            "script_args": [
                "build_ext",
                "--build-temp",
                str(TEMP),
                "--build-lib",
                str(STAGE),
                "--force",
            ],
        }
    )
    distribution.parse_command_line()
    distribution.run_commands()


def _verify_stage() -> None:
    """在独立进程里验证 staging 能导入编译后的业务包。"""
    import subprocess

    check = (
        "import sys; "
        f"sys.path.insert(0, {str(STAGE)!r}); "
        "import api.mcp_api as api, core.tia_service as service; "
        "print('cython stage ok:', api.__file__)"
    )
    subprocess.run([sys.executable, "-c", check], check=True, cwd=STAGE)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cython 编译 TIA Portal V21 MCP")
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="保留 build/cython-temp 中的 C 源码与目标文件",
    )
    args = parser.parse_args()

    modules = _stage_sources()
    print(f"待编译模块 {len(modules)} 个，输出目录：{STAGE}")
    _build_extensions(modules)
    _verify_stage()

    if not args.keep_temp and TEMP.exists():
        shutil.rmtree(TEMP)

    print("")
    print("Cython staging 就绪：", STAGE)
    print("下一步手动打包：")
    print(
        f'  "{sys.executable}" -m PyInstaller --noconfirm --clean '
        f'"{PROJECT_ROOT / "build" / "tia_portal_v21_mcp.spec"}"'
    )


if __name__ == "__main__":
    main()
