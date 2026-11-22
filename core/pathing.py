"""运行位置无关的文件路径校验。"""

from __future__ import annotations

import sys
from pathlib import Path


if getattr(sys, "frozen", False):
    # PyInstaller onedir 把扩展模块放在 release/_internal，真正的安装目录
    # 应以可执行文件所在位置为准。
    SERVICE_DIRECTORY = Path(sys.executable).resolve().parent
else:
    SERVICE_DIRECTORY = Path(__file__).resolve().parents[1]


def absolute_path(value: str | Path, field: str) -> Path:
    """文件工具只接受显式绝对路径，避免依赖服务进程工作目录。"""
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError(f"{field} 必须是非空绝对路径。")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field} 必须是绝对路径，不能相对于 MCP 服务工作目录。")
    return path.resolve()


def output_path(value: str | Path, field: str) -> Path:
    """持久文件输出必须位于服务安装目录之外。"""
    path = absolute_path(value, field)
    if path == SERVICE_DIRECTORY or SERVICE_DIRECTORY in path.parents:
        raise ValueError(f"{field} 不能位于 MCP 服务安装目录内：{SERVICE_DIRECTORY}")
    return path
