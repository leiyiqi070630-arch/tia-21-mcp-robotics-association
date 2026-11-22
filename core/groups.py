"""程序块分组（目录）创建。

桥接层没有暴露 ``PlcBlockGroup.Groups.Create``，但验证过一条可靠路径：
把空的 FC 模板放进目标目录层级再 ``import_blocks``，TIA 会创建分组并把块放进
分组；删除标记块后分组仍然保留。本模块把这条路径封装成幂等工具。
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


# 源码运行：<project>/device_templates；打包后：<_internal>/device_templates。
GROUP_MARKER_TEMPLATE = (
    Path(__file__).resolve().parents[1] / "device_templates" / "group_marker.xml"
)
TEMPLATE_BLOCK_NAME = "MCP_GroupMarkerTemplate"
INVALID_GROUP_CHARS = set('<>:"\\|?*')


def normalize_group_path(group_path: str) -> str:
    """校验并归一化 ``A/B`` 形式的分组路径。"""
    if not isinstance(group_path, str) or not group_path.strip():
        raise ValueError("group_path 必须是非空字符串，例如 '通信/Modbus'。")
    text = group_path.strip().replace("\\", "/")
    segments = text.split("/")
    if any(not segment.strip() for segment in segments):
        raise ValueError("group_path 不能有空的分组名，也不能以 / 开头或结尾。")
    cleaned: list[str] = []
    for segment in segments:
        name = segment.strip()
        if name in {".", ".."}:
            raise ValueError("group_path 不能包含 . 或 .. 分组名。")
        if any(character in INVALID_GROUP_CHARS for character in name):
            raise ValueError(f"分组名 {name!r} 包含非法字符：{''.join(sorted(INVALID_GROUP_CHARS))}")
        if len(name) > 128:
            raise ValueError(f"分组名过长（最多 128 字符）：{name!r}")
        cleaned.append(name)
    return "/".join(cleaned)


def _group_exists(plc: Any, group_path: str) -> bool:
    """分组存在（含空分组）时返回 True；不存在的路径会抛 NullReference。"""
    try:
        plc.get_program_blocks(folder_path=group_path)
        return True
    except Exception as error:
        text = str(error).casefold()
        if "nullreference" in text or "未将对象引用" in text or "does not exist" in text:
            return False
        raise


def create_program_block_group(
    service: Any,
    group_path: str,
    plc_name: str | None = None,
    confirm_create: bool = False,
) -> dict[str, Any]:
    """创建（可多级）程序块分组并命名；已存在时幂等返回。

    必须离线。实现方式：把空 FC 标记块按目标目录层级导入，TIA 自动创建分组；
    删除标记块后分组保留为空分组。
    """
    if not confirm_create:
        raise ValueError("创建程序块分组必须设置 confirm_create=true。")
    normalized = normalize_group_path(group_path)
    segments = normalized.split("/")

    with service._lock:
        plc = service._require_plc(plc_name)
        service._assert_offline(plc)
        if _group_exists(plc, normalized):
            return {
                "ok": True,
                "created": False,
                "already_exists": True,
                "group": normalized,
                "segments": segments,
                "message": "分组已存在，未做修改。",
            }

        if not GROUP_MARKER_TEMPLATE.is_file():
            raise FileNotFoundError(
                f"缺少分组标记模板：{GROUP_MARKER_TEMPLATE}；"
                "请确认 device_templates/group_marker.xml 随服务一起分发。"
            )

        marker = f"MCP_GroupMarker_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        template = GROUP_MARKER_TEMPLATE.read_text(encoding="utf-8-sig")
        if TEMPLATE_BLOCK_NAME not in template:
            raise RuntimeError(
                f"分组标记模板缺少块名 {TEMPLATE_BLOCK_NAME!r}，模板可能已损坏。"
            )
        source = template.replace(TEMPLATE_BLOCK_NAME, marker)

        with tempfile.TemporaryDirectory(prefix="tia_mcp_group_") as temp:
            root = Path(temp)
            folder = root.joinpath(*segments)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{marker}.xml").write_text(
                source, encoding="utf-8-sig", newline="\n"
            )
            ts = service._library()
            try:
                plc.import_blocks(
                    import_root_directory=str(root),
                    import_options=ts.Enums.GeneralImportOptions.Override,
                )
            except Exception as error:
                raise RuntimeError(
                    f"创建分组 {normalized!r} 失败：TIA 导入标记块时出错：{error}"
                ) from error

            blocks = plc.get_program_blocks(folder_path=normalized)
            target = next(
                (block for block in blocks if str(block.get_name()) == marker), None
            )
            if target is None:
                raise RuntimeError(
                    f"分组 {normalized!r} 创建后未找到标记块 {marker!r}；"
                    "分组可能未创建成功。"
                )
            target.delete()

        if not _group_exists(plc, normalized):
            raise RuntimeError(
                f"标记块删除后分组 {normalized!r} 消失，未完成创建。"
            )

        return {
            "ok": True,
            "created": True,
            "already_exists": False,
            "group": normalized,
            "segments": segments,
            "marker_removed": True,
            "empty": True,
            "saved": False,
            "message": f"已创建空分组 {normalized!r}；如需保存请调用 save_project。",
        }
