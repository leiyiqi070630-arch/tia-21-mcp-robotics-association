"""TIA MCP 2.0.0 新增能力：工程管理、库复用、交叉引用与块保护。

这些函数直接调用 ``siemens_tia_scripting`` 暴露的 Openness 封装，由
``api/mcp_api.py`` 里的同名工具通过 ``_tia_call`` 在 TIA 专用线程中执行。
所有函数都遵循主服务的三条约定：

1. 调用方必须已连接工程（否则 ``_require_project`` 抛错）；
2. 会写工程的动作要求显式 ``confirm_*`` 参数，并在需要时先确认 PLC 离线；
3. 版本差异统一经 ``_require_version`` 降级为可读错误，不让
   ``AttributeError`` 直接冒到客户端。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.pathing import absolute_path, output_path
from core.tia_service import _safe_properties


# ---------------------------------------------------------------- 版本与校验


def _version_number(value: Any) -> tuple[int, int]:
    """把 ``21.0`` / ``21.1`` 之类的字符串转成可比较的二元组。"""
    text = str(value or "").strip()
    parts = text.split(".")
    try:
        major = int(parts[0])
    except (ValueError, IndexError):
        major = 0
    try:
        minor = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        minor = 0
    return major, minor


def _version_at_least(service: Any, minimum: str) -> bool:
    """当前连接版本是否满足最低要求。"""
    try:
        version = service.connected_tia_version()
    except Exception:
        return True  # 版本未知时不阻断，交给底层报错
    return _version_number(version.api_version) >= _version_number(minimum)


def _require_version(service: Any, minimum: str, feature: str) -> None:
    """版本不足时给出明确说明，而不是让 AttributeError 冒出去。"""
    if _version_at_least(service, minimum):
        return
    version = service.connected_tia_version()
    raise RuntimeError(
        f"{feature} 需要 TIA Portal {minimum} 及以上，当前连接为 {version.label}。"
        "请在支持的版本上使用该功能，或改在 TIA 界面中人工操作。"
    )


def _find_block(service: Any, plc: Any, block_name: str) -> Any:
    """按名称取一个程序块；找不到时列出可选项。"""
    blocks = list(plc.get_program_blocks())
    wanted = str(block_name or "").strip()
    for block in blocks:
        if str(block.get_name()).casefold() == wanted.casefold():
            return block
    available = [str(block.get_name()) for block in blocks]
    raise RuntimeError(f"工程中不存在程序块 {block_name!r}；可选项：{available}")


def _file_summary(path: Path) -> dict[str, Any]:
    """返回文件是否存在、大小与时间戳。"""
    if not path.exists():
        return {"exists": False}
    stat = path.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "modified": int(stat.st_mtime),
    }


# ---------------------------------------------------------------- 工程管理


def update_module_descriptions(
    service: Any, scope: str = "plc", plc_name: str | None = None
) -> dict[str, Any]:
    """刷新模块描述（硬件目录更新后修复「模块不可用」）。"""
    _require_version(service, "21.0", "刷新模块描述")
    with service._lock:
        project = service._require_project()
        normalized = str(scope or "plc").strip().casefold()
        if normalized == "project":
            project.update_module_description()
            target = "project"
        elif normalized == "plc":
            plc = service._require_plc(plc_name)
            plc.update_module_description()
            target = f"PLC {plc.get_name()}"
        else:
            raise ValueError("scope 只能是 project 或 plc。")
        return {
            "ok": True,
            "target": target,
            "scope": normalized,
            "message": "已请求 TIA 刷新模块描述；若硬件组态仍有旧型号，请重新打开设备视图确认。",
        }


def export_project_texts(
    service: Any,
    target_file: str,
    source_language: str | None = None,
    target_language: str | None = None,
) -> dict[str, Any]:
    """导出工程多语言文本（注释、标题、HMI 文本），用于翻译或评审。"""
    _require_version(service, "21.0", "导出工程文本")
    with service._lock:
        project = service._require_project()
        target = output_path(target_file, "target_file")
        target.parent.mkdir(parents=True, exist_ok=True)
        project.export_project_texts(
            str(target), source_language or "", target_language or ""
        )
        return {
            "ok": True,
            "file": str(target),
            "source_language": source_language,
            "target_language": target_language,
            **_file_summary(target),
        }


def import_project_texts(
    service: Any,
    source_file: str,
    update_source_language: bool = False,
    confirm_import: bool = False,
) -> dict[str, Any]:
    """把翻译后的工程文本导回工程（需确认）。"""
    _require_version(service, "21.0", "导入工程文本")
    if not confirm_import:
        raise ValueError("导入工程文本必须设置 confirm_import=true。")
    with service._lock:
        project = service._require_project()
        source = Path(source_file).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"找不到工程文本文件：{source}")
        project.import_project_texts(str(source), bool(update_source_language))
        return {
            "ok": True,
            "file": str(source),
            "update_source_language": bool(update_source_language),
            "message": "文本已导入；请用 compile_plc 复核后再保存。",
        }


def save_project_as(
    service: Any,
    target_directory: str,
    project_name: str,
    overwrite: bool = False,
    confirm_save_as: bool = False,
) -> dict[str, Any]:
    """把当前工程另存为新工程（副本），用于版本分支或交付备份。

    TIA 的 Save As 会把当前打开的工程切换成副本，因此返回里明确提示
    需要用 connect_tia 重新指向原工程。
    """
    if not confirm_save_as:
        raise ValueError("工程另存为必须设置 confirm_save_as=true。")
    with service._lock:
        project = service._require_project()
        target = output_path(target_directory, "target_directory")
        target.mkdir(parents=True, exist_ok=True)
        name = str(project_name or "").strip()
        if not name:
            raise ValueError("project_name 不能为空。")
        existing = target / name
        if existing.exists() and not overwrite:
            raise FileExistsError(
                f"目标工程已存在：{existing}；如需覆盖请设置 overwrite=true。"
            )
        before = {str(path) for path in target.rglob("*.ap21")}
        project.save_as(str(target), name)
        created = sorted({str(path) for path in target.rglob("*.ap21")} - before)
        # Save As 会把当前会话切到副本；尽量刷新服务里的工程引用，失败也不影响结果。
        refreshed = False
        try:
            service.project = service.portal.get_project()
            refreshed = True
        except Exception:
            refreshed = False
        project_file = created[0] if created else ""
        return {
            "ok": True,
            "project_name": name,
            "target_directory": str(target),
            "project_file": project_file,
            "created_files": created,
            **( _file_summary(Path(project_file)) if project_file else {}),
            "session_followed_copy": refreshed,
            "warning": (
                "TIA 已切换到另存后的副本；继续操作原工程前，请重新调用 "
                "connect_tia(project_file_path=<原工程路径>)。"
            ),
        }


# ---------------------------------------------------------------- 库与复用


def _describe_library_type(library_type: Any) -> dict[str, Any]:
    """把库类型整理成名称 + 版本 + 状态。"""
    versions: list[dict[str, Any]] = []
    try:
        for version in library_type.get_versions():
            item: dict[str, Any] = {
                "version": str(version.get_version_number()),
            }
            for key in ("get_state", "get_modified_date", "get_author"):
                try:
                    item[key[4:]] = str(getattr(version, key)())
                except Exception:
                    continue
            versions.append(item)
    except Exception as error:  # 类型没有版本（未发布）时也不影响整体列表
        versions = [{"error": str(error)}]
    return {"name": str(library_type.get_name()), "versions": versions}


def list_libraries(service: Any) -> dict[str, Any]:
    """列出全局库、项目库类型与主副本，用于判断有哪些可复用资产。"""
    with service._lock:
        portal = service.portal
        project = service._require_project()
        result: dict[str, Any] = {
            "ok": True,
            "global_libraries": [],
            "project_library": {"types": [], "master_copies": []},
        }
        try:
            result["global_libraries"] = [
                {"name": str(info.get_name())}
                for info in (portal.get_global_library_infos() or [])
            ]
        except Exception as error:
            result["global_libraries_error"] = str(error)
        try:
            library = project.get_project_library()
            result["project_library"]["types"] = [
                _describe_library_type(item) for item in (library.get_types() or [])
            ]
            result["project_library"]["master_copies"] = [
                {
                    "name": str(item.get_name()),
                    "content_name": str(item.get_content_name()),
                    "content_type": str(item.get_content_type()),
                }
                for item in (library.get_master_copies() or [])
            ]
        except Exception as error:
            result["project_library"]["error"] = str(error)
        return result


def create_global_library(
    service: Any,
    target_directory: str,
    library_name: str,
    overwrite: bool = False,
    confirm_create: bool = False,
) -> dict[str, Any]:
    """新建全局库（.al21），用于跨工程复用。"""
    if not confirm_create:
        raise ValueError("创建全局库必须设置 confirm_create=true。")
    name = str(library_name or "").strip()
    if not name:
        raise ValueError("library_name 必须是非空字符串。")
    if any(character in name for character in '<>:"/\\|?*'):
        raise ValueError("library_name 不能包含 Windows 文件名禁用字符。")
    with service._lock:
        portal = service.portal
        if portal is None:
            raise RuntimeError("尚未连接 TIA Portal，请先调用 connect_tia。")
        target = output_path(target_directory, "target_directory")
        target.mkdir(parents=True, exist_ok=True)
        library = portal.create_global_library(
            target_directory_path=str(target),
            library_name=name,
            delete_existing_project=bool(overwrite),
        )
        if library is None:
            raise RuntimeError("create_global_library 未返回可用的全局库对象。")
        saved = False
        try:
            library.save()
            saved = True
        except Exception:
            # 新建库通常已经落盘，保存失败不阻断返回，但要在结果里说明。
            pass
        return {
            "ok": True,
            "created": True,
            "library": str(library.get_name()),
            "path": str(library.get_path()),
            "target_directory": str(target),
            "overwritten": bool(overwrite),
            "saved": saved,
        }


def open_global_library(
    service: Any,
    library_path: str,
    confirm_open: bool = False,
) -> dict[str, Any]:
    """打开已有全局库（.al21）。"""
    if not confirm_open:
        raise ValueError("打开全局库必须设置 confirm_open=true。")
    path = absolute_path(library_path, "library_path")
    if not path.is_file():
        raise FileNotFoundError(f"找不到全局库文件：{path}")
    with service._lock:
        portal = service.portal
        if portal is None:
            raise RuntimeError("尚未连接 TIA Portal，请先调用 connect_tia。")
        library = portal.open_global_library(library_path=str(path))
        if library is None:
            raise RuntimeError("open_global_library 未返回可用的全局库对象。")
        return {
            "ok": True,
            "opened": True,
            "library": str(library.get_name()),
            "path": str(path),
            "read_only": bool(library.is_read_only()),
        }


def create_master_copy(
    service: Any,
    block_name: str,
    library_folder_path: str | None = None,
    plc_name: str | None = None,
    confirm_create: bool = False,
) -> dict[str, Any]:
    """把程序块存成主副本（进当前工程库），供后续复用/实例化。"""
    _require_version(service, "21.0", "创建主副本")
    if not confirm_create:
        raise ValueError("创建主副本必须设置 confirm_create=true。")
    with service._lock:
        plc = service._require_plc(plc_name)
        service._assert_offline(plc)
        block = _find_block(service, plc, block_name)
        copy = block.create_master_copy(library_folder_path or "")
        return {
            "ok": True,
            "block": str(block.get_name()),
            "master_copy": str(copy.get_name()),
            "content_name": str(copy.get_content_name()),
            "content_type": str(copy.get_content_type()),
            "library_folder_path": library_folder_path,
        }



# ---------------------------------------------------------------- 高速计数器（只读）


_HSC_NAME = re.compile(r"(?i)(hsc|counter|计数)")
_COUNT_ADDRESS = re.compile(r"(?i)^%ID\s*\d+$")


def read_hsc_configuration(
    service: Any, plc_name: str | None = None
) -> dict[str, Any]:
    """读取已人工组态的高速计数器可见信息，供生成程序时使用。

    只读操作。Openness 不暴露 HSC 深层参数页，因此这里能拿到的是：
    系统常量里的 ``HSC_*`` 硬件标识、变量表里与计数相关的符号及其地址。
    计数模式/滤波/比较值等必须由调用方补充。
    """
    with service._lock:
        plc = service._require_plc(plc_name)
        tables = list(plc.get_plc_tag_tables())
        system_constants: list[dict[str, Any]] = []
        tags: list[dict[str, Any]] = []
        for table in tables:
            table_name = str(table.get_name())
            try:
                for const in table.get_system_constants() or []:
                    system_constants.append({
                        "table": table_name,
                        "name": str(const.get_name()),
                        "properties": _safe_properties(const),
                    })
            except Exception as error:
                system_constants.append({
                    "table": table_name,
                    "error": str(error),
                })
            try:
                for tag in table.get_plc_tags() or []:
                    tags.append({
                        "table": table_name,
                        "name": str(tag.get_name()),
                        "properties": _safe_properties(tag),
                    })
            except Exception:
                continue

        hsc_ids = [
            item for item in system_constants
            if "error" not in item and _HSC_NAME.search(item["name"])
        ]
        counter_tags = [
            item for item in tags
            if _HSC_NAME.search(item["name"])
            or _COUNT_ADDRESS.match(str(item["properties"].get("LogicalAddress", "")))
        ]
        return {
            "ok": True,
            "plc": str(plc.get_name()),
            "hsc_hardware_identifiers": hsc_ids,
            "counter_tags": counter_tags,
            "system_constant_count": len(system_constants),
            "tag_count": len(tags),
            "unreadable": [
                "计数模式（单相/AB 相/双相）",
                "输入滤波与输入分配（复位/方向）",
                "比较值与比较输出",
                "HSC 使能状态与计数方向",
            ],
            "guidance": (
                "把 hsc_hardware_identifiers 里的常量值作为 CTRL_HSC 的 HSC 参数；"
                "计数模式与比较值读不到，调用方必须显式提供（或按现场点表确认）。"
            ),
        }

# ---------------------------------------------------------------- 交叉引用与保护


def export_cross_references(
    service: Any,
    target_directory: str,
    block_names: list[str] | None = None,
    cross_reference_filter: int = 0,
    plc_name: str | None = None,
) -> dict[str, Any]:
    """导出程序块的交叉引用（谁调用了它、它用到了哪些全局符号）。"""
    _require_version(service, "21.0", "导出交叉引用")
    with service._lock:
        plc = service._require_plc(plc_name)
        blocks = service._select_named(
            list(plc.get_program_blocks()), block_names
        )
        destination = output_path(target_directory, "target_directory")
        destination.mkdir(parents=True, exist_ok=True)
        before = {str(path) for path in destination.rglob("*") if path.is_file()}
        exported: list[str] = []
        for block in blocks:
            block.export_cross_references(
                str(destination), int(cross_reference_filter)
            )
            exported.append(str(block.get_name()))
        after = {str(path) for path in destination.rglob("*") if path.is_file()}
        return {
            "ok": True,
            "target_directory": str(destination),
            "exported": exported,
            "created_files": sorted(after - before),
        }


def set_block_protection(
    service: Any,
    block_name: str,
    action: str = "set",
    password: str | None = None,
    plc_name: str | None = None,
    confirm_change: bool = False,
) -> dict[str, Any]:
    """给程序块加/解除专有技术保护（know-how protection）。"""
    _require_version(service, "21.0", "专有技术保护")
    if not confirm_change:
        raise ValueError("修改块保护必须设置 confirm_change=true。")
    normalized = str(action or "set").strip().casefold()
    if normalized not in {"set", "remove"}:
        raise ValueError("action 只能是 set 或 remove。")
    if not password:
        raise ValueError("设置或解除块保护都必须提供 password。")
    with service._lock:
        plc = service._require_plc(plc_name)
        service._assert_offline(plc)
        block = _find_block(service, plc, block_name)
        if normalized == "set":
            block.set_know_how_protection(str(password))
        else:
            block.remove_know_how_protection(str(password))
        return {
            "ok": True,
            "block": str(block.get_name()),
            "action": normalized,
            "message": (
                "已加密该块；后续修改需先 remove 或提供密码。"
                if normalized == "set"
                else "已解除该块的专有技术保护。"
            ),
        }
