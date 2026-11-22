from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.elicitation import render_elicitation_schema
from mcp.types import (
    ElicitRequest,
    ElicitRequestFormParams,
    InputRequiredResult,
    ToolAnnotations,
)
from pydantic import BaseModel, Field

from core.tia_service import TiaService
from core.lad_builder import render_lad_block
from core.ob_builder import (
    assert_lad_ob_xml,
    build_lad_ob_xml,
    default_ob_skeleton,
    extract_flgnet,
)
from core.output_templates import output_template_hint, read_output_template
from core.programming_policy import BLOCK_MAIN, programming_hint, programming_policy
from core.pathing import output_path
from core.tia_versions import parse_tia_version
from core import knowledge_base as local_knowledge
from core import tia_extras as extras
from core import user_actions
from core import security as plc_security
from core import groups as plc_groups


service = TiaService()
_TIA_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tia-mcp")

# --- 运行期状态（服务启动时间、工具注册表） ---
_STARTED_AT = time.time()
_TOOL_REGISTRY: dict[str, Any] = {}

# 这些工具的描述里会前置控制台的编程偏好，模型拉取工具列表即可看到。
POLICY_HINT_TOOLS = frozenset(
    {
        "write_lad_block",
        "write_program_blocks",
        "prepare_lad_block",
        "get_default_programming_policy",
    }
)

# 连接类工具的描述里前置输出模板提示：模型连上 TIA 后按控制台配置的模板汇报。
TEMPLATE_HINT_TOOLS = frozenset(
    {
        "connect_tia",
        "get_tia_status",
        "get_output_template",
    }
)

# 编程入口与知识库工具的描述里前置知识库状态，避免客户端忽略 Server instructions。
KNOWLEDGE_HINT_TOOLS = frozenset(
    {
        "get_default_programming_policy",
        "get_knowledge_base_status",
        "list_knowledge_documents",
        "search_knowledge_base",
        "read_knowledge_document",
        "write_lad_block",
        "write_program_blocks",
        "prepare_lad_block",
    }
)

# SCL 外源里的组织块声明（ORGANIZATION_BLOCK "Main"）：用于拦下「用 SCL 写 OB」。
_SCL_OB_PATTERN = re.compile(r"(?im)^\s*ORGANIZATION_BLOCK\b")


def registered_tool_names() -> list[str]:
    """返回已注册工具名（同步）。

    启动横幅、``--info`` 与桌面控制台都从这里取工具数量，避免新增工具时
    还要手动同步一个常量。
    """
    return sorted(_TOOL_REGISTRY)


async def get_service_info() -> dict[str, Any]:
    """返回服务版本、工具数量、运行时长与已注册工具清单。"""
    from mcp_run import MCP_VERSION

    return {
        "ok": True,
        "version": MCP_VERSION,
        "pid": os.getpid(),
        "tool_count": len(_TOOL_REGISTRY),
        "uptime_seconds": round(time.time() - _STARTED_AT, 1),
        "endpoint": "http://127.0.0.1:8000/mcp",
        "tools": registered_tool_names(),
    }


def _error_result(error: Exception) -> dict[str, Any]:
    """把异常转成结构化错误，避免客户端只看到一句「工具执行失败」。

    命中「MCP 无法自动完成」的 TIA 场景时，额外附带 needs_user_action 与
    可执行步骤，供 AI 原样转告用户。
    """
    result: dict[str, Any] = {
        "ok": False,
        "error_type": type(error).__name__,
        "error": str(error) or repr(error),
    }
    guide = user_actions.explain_message(result["error"])
    if guide is not None:
        result["needs_user_action"] = True
        result["user_action"] = guide
        result["user_message"] = user_actions.format_user_message(guide)
    return result


def _lad_rule_errors(compilation: Any) -> list[str]:
    """挑出「网络不符合 LAD 规则」这类会让 TIA 渲染崩溃的编译诊断。"""
    software = (compilation or {}).get("software") or {}
    found: list[str] = []
    messages = list(software.get("errors") or []) + list(
        software.get("all_messages") or []
    )
    for message in messages:
        text = str(message)
        if not any(marker in text for marker in ("rules for LAD", "does not comply")):
            continue
        if text not in found:
            found.append(text)
    return found


# 编译结果里夹带的汇总行，不属于任何块的诊断，分类前先剔除。
_COMPILE_NOISE_PREFIXES = (
    "compiling finished",
    "no block was compiled",
    "hardware was not compiled",
)


def _compile_messages(compilation: Any) -> list[str]:
    """取出一次编译结果里的全部消息（errors + all_messages，去重保序）。

    「Compiling finished (errors: …)」「No block was compiled」这类汇总行会被剔除，
    避免它们被当成某个块的问题混进 ``project_errors``。
    """
    software = (compilation or {}).get("software") or {}
    messages: list[str] = []
    for message in list(software.get("errors") or []) + list(
        software.get("all_messages") or []
    ):
        text = str(message).strip()
        if not text or text.casefold().startswith(_COMPILE_NOISE_PREFIXES):
            continue
        if text not in messages:
            messages.append(text)
    return messages


def _split_compile_errors(
    compilation: Any, temp_name: str
) -> tuple[list[str], list[str]]:
    """把编译消息分成「临时参考块引起」和「工程其它块引起」两组。

    TIA 的编译消息以 ``PLC > Program blocks > 块名 (类型) > 网络`` 开头，因此按
    临时块名归属是可靠的：含临时块名的属于本次规格，其余属于工程既有问题。
    """
    spec_errors: list[str] = []
    project_errors: list[str] = []
    for text in _compile_messages(compilation):
        lowered = text.casefold()
        # “Block was successfully compiled.” 也带块名；它不是错误，不能算进
        # spec_errors，否则会把「参考块通过、工程另有错误」误报成规格失败。
        if "was successfully compiled" in lowered or "all blocks are up-to-date" in lowered:
            continue
        if temp_name.casefold() in lowered or "mcp_ladobtemp" in lowered:
            spec_errors.append(text)
        else:
            project_errors.append(text)
    return spec_errors, project_errors


def _calls_without_instance(networks: Any) -> list[str]:
    """挑出规格里「调块但没给 instance」的 call：最容易踩的坑。"""
    found: list[str] = []
    if not isinstance(networks, list):
        return found
    for network in networks:
        rungs = (network or {}).get("rungs") if isinstance(network, dict) else None
        if not isinstance(rungs, list):
            continue
        for rung in rungs:
            steps = (rung or {}).get("steps") if isinstance(rung, dict) else None
            if not isinstance(steps, list):
                continue
            for step in steps:
                if not isinstance(step, dict):
                    continue
                call = step.get("call")
                if not isinstance(call, dict) or call.get("instance"):
                    continue
                block = str(call.get("block") or "").strip() or "<未指定块>"
                found.append(f"{block}（未指定 instance）")
    return found


def _reference_failure_steps(
    call_warnings: list[str], spec_errors: list[str]
) -> list[str]:
    """参考块编译失败时给出可执行的下一步。"""
    steps: list[str] = []
    if call_warnings:
        steps.append(
            "spec 里的 call 未指定 instance："
            + "；".join(call_warnings[:3])
            + "。临时参考块是 FC，调用 FB 必须提供已存在的实例 DB——先用 "
            'write_program_blocks 建单实例 DB，再把 call 写成 '
            '{"block": "FB_X", "instance": "DB_X"}。'
        )
    if spec_errors:
        steps.append(
            "按 spec_errors 里指出的块/网络修改规格（检查操作数是否都在变量表里、"
            "并联支路是否合法），再重试。"
        )
    if not steps:
        steps.append(
            "检查 spec 的操作数是否都在变量表里、网络结构是否符合 LAD 规则，再重试。"
        )
    return steps


def _apply_default_lad_policy(
    spec: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """为显式 LAD 写入补齐当前块架构偏好与中文注释。"""
    if not isinstance(spec, dict):
        raise ValueError("spec 必须是对象。")
    normalized = copy.deepcopy(spec)
    defaults_applied: list[str] = []
    policy = programming_policy()
    prefer_main = policy["preferences"]["preferred_block"] == BLOCK_MAIN
    if not str(normalized.get("kind") or "").strip():
        normalized["kind"] = "OB" if prefer_main else "FC"
        defaults_applied.append(f"kind={normalized['kind']}")
    kind = str(normalized["kind"]).strip().upper()
    if kind == "OB" and not str(normalized.get("name") or "").strip():
        normalized["name"] = "Main"
        defaults_applied.append("name=Main (OB1)")
    elif kind in {"FB", "FC"} and not str(normalized.get("name") or "").strip():
        normalized["name"] = "FB_MainLogic" if kind == "FB" else "FC_MainLogic"
        defaults_applied.append(f"name={normalized['name']}")
    networks = normalized.get("networks")
    if isinstance(networks, list):
        for index, network in enumerate(networks, 1):
            if not isinstance(network, dict):
                continue
            if not str(network.get("title") or "").strip():
                network["title"] = f"网络 {index}"
                defaults_applied.append(f"network[{index}].title=中文")
            if not str(network.get("comment") or "").strip():
                network["comment"] = f"网络 {index}：执行用户要求的梯形图逻辑。"
                defaults_applied.append(f"network[{index}].comment=中文")
    applied = dict(policy)
    # 此函数属于 write_lad_block：即使全局偏好为 SCL，显式调用仍然必须写 LAD。
    applied["language"] = "LAD"
    applied["language_label"] = "梯形图"
    applied["primary_block"] = "Main (OB1)" if prefer_main else "FB/FC"
    applied["defaults_applied"] = defaults_applied
    return normalized, applied


async def _tia_call(function: Callable[..., Any], *args: Any) -> Any:
    """将所有 TIA 原生调用安排到同一个专用线程。

    异常统一转成结构化结果，否则客户端只会看到「工具执行失败」，
    丢掉服务层给出的可读原因（例如「修改工程前请先调用 go_offline」）。
    """
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_TIA_EXECUTOR, partial(function, *args))
    except Exception as error:
        return _error_result(error)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
OPEN_WORLD = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, openWorldHint=True
)


async def list_installed_tia_products() -> dict[str, Any]:
    """列出本机已安装的 TIA Portal 产品和产品包。"""
    return await _tia_call(service.installed_products)


async def check_openness_prerequisites() -> dict[str, Any]:
    """检查当前用户是否属于 Siemens TIA Openness 组，并返回明确修复步骤。"""
    return await _tia_call(service.openness_prerequisites)


async def get_openness_access_guide(
    version: str | None = None,
) -> dict[str, Any]:
    """说明首次连接或更新 EXE 后出现的 Openness 安全授权窗口。"""
    return await _tia_call(service.openness_access_guide, version)


async def list_running_tia_portals(
    version: str = "auto",
    portal_mode: str = "any-ui",
    probe_openness: bool = True,
) -> dict[str, Any]:
    """列出活动 TIA PID；probe_openness=false 时只读 Windows 进程表，速度最快。"""
    return await _tia_call(
        service.running_tia_portals, version, portal_mode, probe_openness
    )


async def get_project_creation_capabilities() -> dict[str, Any]:
    """说明项目创建、目录设备创建和已有设备组态分别是否受当前桥接层支持。"""
    return await _tia_call(service.project_creation_capabilities)


async def create_tia_project(
    target_directory: str,
    project_name: str,
    version: str = "auto",
    portal_mode: str = "gui",
    overwrite_existing: bool = False,
    confirm_create: bool = False,
) -> dict[str, Any]:
    """创建 TIA Portal V21 工程；version 只接受 auto / 21.0 / 21.1。

    其它版本一律拒绝。若当前已有打开工程，会新开 V21 Portal，绝不替用户关闭现有工程。
    当前桥接层不能从硬件目录自动新增设备；创建后请查看返回的能力边界。
    """
    return await _tia_call(
        service.create_project,
        target_directory,
        project_name,
        version,
        portal_mode,
        overwrite_existing,
        confirm_create,
    )


async def export_project_aml(target_file: str) -> dict[str, Any]:
    """把当前工程导出为 CAx(AML)。导出的文件可直接当作新建设备的模板。"""
    return await _tia_call(service.export_project_aml, target_file)


async def add_device_from_aml(
    aml_path: str,
    ip_address: str | None = None,
    subnet_mask: str | None = None,
    confirm_import: bool = False,
) -> dict[str, Any]:
    """用 CAx(AML) 在当前工程新建设备，并可在导入前改写 PROFINET IP 与掩码。

    桥接层没有 CreateWithItem，这是真正能建出设备与模块的唯一通道。
    必须先 bring 工程离线；导入后不会自动保存。
    """
    return await _tia_call(
        service.add_device_from_aml,
        aml_path,
        ip_address,
        subnet_mask,
        confirm_import,
    )


async def add_device_from_template(
    order_number: str,
    ip_address: str | None = None,
    subnet_mask: str | None = None,
    template_directory: str | None = None,
    confirm_import: bool = False,
) -> dict[str, Any]:
    """按订货号在模板目录中匹配 AML 并新建设备（含 IP）。"""
    return await _tia_call(
        service.add_device_from_template,
        order_number,
        ip_address,
        subnet_mask,
        template_directory,
        confirm_import,
    )


async def list_device_templates(
    template_directory: str | None = None,
) -> dict[str, Any]:
    """列出可用 AML 设备模板及其订货号、网络地址。"""
    return await _tia_call(service.list_device_templates, template_directory)


async def detect_tia_installations() -> dict[str, Any]:
    """汇总本机安装的 TIA 版本与运行实例；只把 V21 列为可连接，其它版本列入 blocked_installed。"""
    return await _tia_call(service.detect_installations)


async def launch_tia(
    version: str = "auto",
    project_file_path: str | None = None,
    wait_seconds: int = 120,
    confirm_launch: bool = False,
    force_new: bool = False,
) -> dict[str, Any]:
    """启动 TIA Portal V21（可选直接打开工程），并把当前连接指向该实例。

    version 只接受 auto / 21.0 / 21.1；仅检测到其它版本时直接报错。
    首次连接或 EXE 变更后仍会弹出 Openness 授权窗口，必须人工确认。
    已有同版本实例时默认复用（不重复启动），force_new=true 才新开。
    """
    return await _tia_call(
        service.launch_tia,
        version,
        project_file_path,
        wait_seconds,
        confirm_launch,
        force_new,
    )


async def get_default_programming_policy() -> dict[str, Any]:
    """读取桌面控制台配置的默认编程语言与块架构偏好。"""
    return await _tia_call(service.default_programming_policy)


async def get_output_template(kind: str = "connect") -> dict[str, Any]:
    """读取桌面控制台「推理设置 → 输出模板」里配置的 Markdown 模板。

    完成 connect_tia 与 get_tia_status 后调用本工具，按返回的 content 中的章节、
    表格与字段输出连接结果；模板里的示例值只是占位，必须替换为本次真实值。
    kind 目前只支持 connect（连接博途输出模板）。
    """
    return read_output_template(kind)


async def get_knowledge_base_status() -> dict[str, Any]:
    """读取控制台「推理设置 → 知识库」配置及当前目录状态。

    未配置或不可读时返回 advisory=true、needs_user_action=false：只告知用户，
    不阻塞本轮任务。
    """
    status = local_knowledge.knowledge_base_status()
    if not status["configured"] or not status["readable"]:
        return local_knowledge.knowledge_base_unavailable(status)
    return {"ok": True, **status}


async def list_knowledge_documents(
    relative_directory: str = "",
    recursive: bool = True,
    limit: int = 200,
) -> dict[str, Any]:
    """列出知识库根目录内支持的文本资料；所有结果均为安全相对路径。

    返回 total_documents / truncated / scan_limited / skipped_files，
    truncated=true 表示还有更多文件未返回。
    """
    return local_knowledge.list_knowledge_documents(relative_directory, recursive, limit)


async def search_knowledge_base(
    query: str,
    relative_directory: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """按关键词检索本地知识库；query 支持空格分隔的多个关键词。

    多个关键词时优先返回全部命中的文件，并返回 matched_terms、
    first_match_offset、total_matches 与 truncated，便于直接读取命中段。
    """
    return local_knowledge.search_knowledge_base(query, relative_directory, limit)


async def read_knowledge_document(
    relative_path: str,
    offset: int = 0,
    limit: int = 20000,
) -> dict[str, Any]:
    """分段读取知识库中的文本资料；relative_path 必须来自列表或检索结果。

    offset 是字符位置（不是行号），可把 first_match_offset 直接作为 offset；
    内容未读完时按 next_offset 续读。
    """
    return local_knowledge.read_knowledge_document(relative_path, offset, limit)


async def connect_tia(
    version: str = "auto",
    portal_mode: str = "any-ui",
    project_file_path: str | None = None,
    plc_name: str | None = None,
    portal_process_id: int | None = None,
) -> dict[str, Any]:
    """连接 TIA Portal V21；version 只接受 auto / 21.0 / 21.1，其它版本会被拒绝。

    先调用 list_running_tia_portals(probe_openness=false) 取得 recommended_process_id，
    再传给 portal_process_id，可避免误控其它实例。底层桥接按版本 attach，本参数负责
    连接前存在性检查与连接后 PID 强校验；不一致时会主动断开。
    首次或更新 EXE 后需人工确认 Openness 授权。
    """
    return await _tia_call(
        service.connect,
        version,
        portal_mode,
        project_file_path,
        plc_name,
        portal_process_id,
    )


async def disconnect_tia(
    close_portal: bool = False, confirm_close: bool = False
) -> dict[str, Any]:
    """断开 MCP 与 TIA 的连接；关闭 TIA 本身需要 confirm_close=true。"""
    return await _tia_call(service.disconnect, close_portal, confirm_close)


async def get_tia_status() -> dict[str, Any]:
    """获取连接、工程、PLC 选择和在线状态。"""
    return await _tia_call(service.status)


async def get_tia_compatibility() -> dict[str, Any]:
    """返回当前连接版本的能力；未连接时返回 V21 能力矩阵与版本策略。"""
    return await _tia_call(service.compatibility)


async def select_plc(plc_name: str) -> dict[str, Any]:
    """在多 PLC 工程中选择后续工具默认使用的目标 PLC。"""
    return await _tia_call(service.select_plc, plc_name)


async def get_device_configuration(
    include_properties: bool = True,
) -> dict[str, Any]:
    """获取当前工程的设备、PLC、HMI 及其可读属性。"""
    return await _tia_call(service.device_configuration, include_properties)


async def get_hardware_inventory(
    include_properties: bool = True,
    property_name_contains: list[str] | None = None,
) -> dict[str, Any]:
    """列出设备、PLC、模块及动态硬件属性；可按属性名片段过滤。"""
    return await _tia_call(
        service.hardware_inventory, include_properties, property_name_contains
    )


async def set_hardware_properties(
    target_type: Literal["plc", "device", "module"],
    target_name: str,
    properties: dict[str, str | bool | int | float],
    plc_name: str | None = None,
    device_name: str | None = None,
    target_identifier: str | None = None,
    verify_readback: bool = True,
    compile_after: bool = True,
    rollback_on_error: bool = True,
    confirm_write: bool = False,
) -> dict[str, Any]:
    """写入动态硬件属性并回读；失败时默认回滚，写入前必须 confirm_write=true。"""
    return await _tia_call(
        service.set_hardware_properties,
        target_type,
        target_name,
        properties,
        plc_name,
        device_name,
        target_identifier,
        verify_readback,
        compile_after,
        rollback_on_error,
        confirm_write,
    )


async def configure_s71200_hsc(
    target_type: Literal["plc", "module"],
    target_name: str,
    hsc_number: int = 1,
    enabled: bool = True,
    clock_input: str = "I0.0",
    property_map: dict[str, str] | None = None,
    plc_name: str | None = None,
    device_name: str | None = None,
    target_identifier: str | None = None,
    compile_after: bool = True,
    confirm_write: bool = False,
) -> dict[str, Any]:
    """使能 S7-1200 HSC 并设置时钟输入；未知型号会返回实际属性候选供映射。"""
    return await _tia_call(
        service.configure_s71200_hsc,
        target_type,
        target_name,
        hsc_number,
        enabled,
        clock_input,
        property_map,
        plc_name,
        device_name,
        target_identifier,
        compile_after,
        confirm_write,
    )


async def find_hardware_property(
    property_name_contains: list[str] | None = None,
    include_values: bool = True,
    target_type: Literal["plc", "device", "module"] | None = None,
    target_name: str | None = None,
    device_name: str | None = None,
) -> dict[str, Any]:
    """在全部可达硬件对象里扫描属性名并返回值；用于写硬件参数前确认参数是否真的可达。

    零命中时返回 filter_matched_nothing 与 recommendation，说明这是桥接层的可达性边界
    （siemens_tia_scripting 只在 Device 上提供 get_modules()，Module 无子项遍历），
    而不是过滤条件太窄。
    """
    return await _tia_call(
        service.find_hardware_property,
        property_name_contains,
        include_values,
        target_type,
        target_name,
        device_name,
    )


async def get_plc_inventory(
    plc_name: str | None = None,
    include_tag_entries: bool = False,
    include_properties: bool = False,
) -> dict[str, Any]:
    """列出 PLC 程序块、变量表、UDT、软件单元、监控表等对象。"""
    return await _tia_call(
        service.plc_inventory, plc_name, include_tag_entries, include_properties
    )


async def list_windows_network_adapters() -> dict[str, Any]:
    """列出 Windows 网卡；go_online 可接受网卡别名或 TIA 中的接口名称。"""
    return await _tia_call(service.network_adapters)


class OnlineConnectionChoice(BaseModel):
    connection_type: Literal["simulation", "physical"] = Field(
        description="simulation=PLCSIM 仿真；physical=物理网卡"
    )


class PhysicalAdapterChoice(BaseModel):
    network_adapter_name: str = Field(
        min_length=1, description="Windows 网卡名称或 TIA PG/PC 接口下拉框中的名称"
    )


def _supports_form_elicitation(ctx: Context) -> bool:
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities else None
    if elicitation is None:
        return False
    # 2025-06-18 的 elicitation: {} 即表示支持表单。
    return ctx.protocol_version == "2025-06-18" or elicitation.form is not None


def _modern_input(
    key: str, message: str, schema: type[BaseModel], state: str
) -> InputRequiredResult:
    """新版 MCP 用 input_required 让客户端展示表单并重试原工具。"""
    return InputRequiredResult(
        input_requests={
            key: ElicitRequest(
                params=ElicitRequestFormParams(
                    message=message,
                    requested_schema=render_elicitation_schema(schema),
                )
            )
        },
        request_state=state,
    )


def _modern_answer(ctx: Context, key: str) -> dict[str, Any] | None:
    response = (ctx.input_responses or {}).get(key)
    if response is None:
        return None
    if response.action != "accept":
        return {"cancelled": True, "action": response.action}
    return response.content or {}


def _resolve_physical_interface(
    requested_name: str, adapters: list[dict[str, Any]]
) -> tuple[str, dict[str, Any] | None]:
    """Windows 别名不能直接作为 TIA 接口名，优先转换成网卡描述名。"""
    for adapter in adapters:
        alias = str(adapter.get("Name", "")).strip()
        description = str(adapter.get("InterfaceDescription", "")).strip()
        if requested_name.casefold() in (alias.casefold(), description.casefold()):
            return description or requested_name, adapter
    # TIA 下拉框中的接口名可能不等于 Windows 网卡名称，允许原样传入。
    return requested_name, None


async def go_online(
    ctx: Context,
    connection_type: Literal["simulation", "physical"] | None = None,
    network_adapter_name: str | None = None,
    target_interface: str | None = None,
    plc_name: str | None = None,
) -> dict[str, Any] | InputRequiredResult:
    """连接 PLC；target_interface 填 PLC 接口名（V21 默认 '1 X1'，不是 IP）。"""
    version_result = await _tia_call(service.connected_tia_version)
    if isinstance(version_result, dict) and version_result.get("ok") is False:
        return version_result
    if target_interface is None:
        target_interface = "1 X1"
    modern = ctx.protocol_version == "2026-07-28"
    if modern and ctx.request_state == "physical_adapter" and connection_type is None:
        connection_type = "physical"
    if connection_type is None:
        if not _supports_form_elicitation(ctx):
            return {
                "ok": False,
                "needs_input": "connection_type",
                "message": "请选择连接方式：simulation（PLCSIM 仿真）或 physical（物理网卡）。",
                "choices": ["simulation", "physical"],
            }
        if modern:
            answer = _modern_answer(ctx, "connection_type")
            if answer is None:
                return _modern_input(
                    "connection_type",
                    "请选择 PLC 在线连接方式。",
                    OnlineConnectionChoice,
                    "connection_type",
                )
            if answer.get("cancelled"):
                return {"ok": False, **answer}
            connection_type = OnlineConnectionChoice.model_validate(answer).connection_type
        else:
            choice = await ctx.elicit("请选择 PLC 在线连接方式。", OnlineConnectionChoice)
            if choice.action != "accept":
                return {"ok": False, "cancelled": True, "action": choice.action}
            connection_type = choice.data.connection_type

    pc_interface_name = "PLCSIM"
    requested_adapter = None
    if connection_type == "physical":
        adapters = (await _tia_call(service.network_adapters))["adapters"]
        if network_adapter_name is None:
            if not _supports_form_elicitation(ctx):
                return {
                    "ok": False,
                    "needs_input": "network_adapter_name",
                    "connection_type": "physical",
                    "message": "请填写物理网卡名称，可用 Windows 名称或 TIA PG/PC 接口名称。",
                    "adapters": adapters,
                }
            names = ", ".join(
                f"{item.get('Name')} ({item.get('InterfaceDescription')}, {item.get('Status')})"
                for item in adapters
            )
            message = f"请填写连接 PLC 的物理网卡名称。Windows 网卡：{names}"
            if modern:
                answer = _modern_answer(ctx, "network_adapter_name")
                if answer is None:
                    return _modern_input(
                        "network_adapter_name",
                        message,
                        PhysicalAdapterChoice,
                        "physical_adapter",
                    )
                if answer.get("cancelled"):
                    return {"ok": False, **answer}
                network_adapter_name = PhysicalAdapterChoice.model_validate(
                    answer
                ).network_adapter_name
            else:
                answer = await ctx.elicit(message, PhysicalAdapterChoice)
                if answer.action != "accept":
                    return {"ok": False, "cancelled": True, "action": answer.action}
                network_adapter_name = answer.data.network_adapter_name

        requested_adapter = network_adapter_name.strip()
        if not requested_adapter:
            return {
                "ok": False,
                "needs_input": "network_adapter_name",
                "message": "物理连接需要非空的网卡名称。",
                "adapters": adapters,
            }
        pc_interface_name, matched = _resolve_physical_interface(
            requested_adapter, adapters
        )
        if matched and str(matched.get("Status", "")).casefold() != "up":
            return {
                "ok": False,
                "error_code": "adapter_not_ready",
                "message": f"网卡 {requested_adapter!r} 当前状态为 {matched.get('Status')}。",
                "adapters": adapters,
            }

    result = await _tia_call(
        service.go_online, "PN/IE", pc_interface_name, target_interface, plc_name
    )
    result["connection_type"] = connection_type
    result["pg_pc_interface"] = pc_interface_name
    result["target_interface"] = target_interface
    if requested_adapter is not None:
        result["requested_network_adapter"] = requested_adapter
    return result


async def go_offline(plc_name: str | None = None) -> dict[str, Any]:
    """将目标 PLC 工程对象转至离线。"""
    return await _tia_call(service.go_offline, plc_name)


async def export_program_blocks(
    target_directory: str,
    block_names: list[str] | None = None,
    export_format: str = "simaticml",
    plc_name: str | None = None,
) -> dict[str, Any]:
    """导出指定程序块；block_names=null 表示全部。"""
    return await _tia_call(
        service.export_program_blocks,
        target_directory,
        block_names,
        export_format,
        plc_name,
    )


async def delete_program_blocks(
    block_names: list[str],
    plc_name: str | None = None,
    backup_before_delete: bool = True,
    backup_directory: str | None = None,
    confirm_delete: bool = False,
) -> dict[str, Any]:
    """删除指定程序块；默认先备份到显式 backup_directory，且必须 confirm_delete=true。"""
    return await _tia_call(
        service.delete_program_blocks,
        block_names,
        plc_name,
        backup_before_delete,
        backup_directory,
        confirm_delete,
    )


async def write_program_blocks(
    source_text: str,
    source_file_name: str = "mcp_source.scl",
    target_folder_path: str | None = None,
    plc_name: str | None = None,
    allow_overwrite: bool = False,
    cleanup_external_source: bool = True,
    confirm_write: bool = False,
    allow_ob_source: bool = False,
) -> dict[str, Any]:
    """把 SCL/SIMATIC SD/XML 文本写入程序块；必须离线并显式确认。

    SCL 外源写 OB 会静默丢掉「优化块访问」属性（S7-1200 G2 上必然编译失败），
    因此默认拒绝；确需该行为时显式传 ``allow_ob_source=true``。OB 的 LAD 请改用
    ``write_lad_block(kind="OB")``，OB 的 Openness XML（导出的 .xml）不受此限制。
    """
    if not allow_ob_source and _SCL_OB_PATTERN.search(str(source_text or "")):
        return _error_result(ValueError(
            "检测到 SCL 组织块（ORGANIZATION_BLOCK）：用 SCL 外源写 OB 会静默丢失"
            "「优化块访问」属性，S7-1200 G2 上必然编译失败（The \"Optimized block "
            "access\" block property must be set to S7-1200 G2 modules for "
            "organization blocks）。改用 write_lad_block(kind=\"OB\") 写 LAD 版 OB；"
            "确需 SCL 写 OB 时显式传 allow_ob_source=true。"
        ))
    return await _tia_call(
        service.write_program_blocks,
        source_text,
        source_file_name,
        target_folder_path,
        plc_name,
        allow_overwrite,
        cleanup_external_source,
        confirm_write,
    )


async def prepare_lad_block(
    spec: dict[str, Any],
    output_directory: str,
    include_source: bool = False,
    target_version: str = "21.0",
) -> dict[str, Any]:
    """把紧凑 LAD 规格转为 .s7dcl；target_version 只接受 V21（21.0 / 21.1）。

    spec 例：{"name":"FB_Motor","kind":"FB","interface":{"input":{"Enable":"Bool"},
    "output":{"Run":"Bool"}},"networks":[{"rungs":[{"steps":[
    {"contact":"#Enable"},{"coil":"#Run"}]}]}]}。
    kind=FB/FC；步骤支持 contact、nc_contact、coil、inverted_coil、wire、
    call、instruction、move、compare 和 ctrl_hsc。compare 必须指定 data_type；
    CTRL_HSC 必须指定实例 DB 与 HSC 硬件标识。每个 rung 可用 start/end 指定分支线名。
    output_directory 必须是服务安装目录外的绝对路径。
    默认只返回文件路径与摘要；include_source=true 才返回全文。
    """
    try:
        version = parse_tia_version(target_version)
    except Exception as error:
        return _error_result(error)
    if str((spec or {}).get("kind", "FB")).upper() == "OB":
        return _error_result(ValueError(
            'prepare_lad_block 只能生成 FB/FC 的 SIMATIC SD。OB 的 LAD 网络必须先由 TIA '
            '生成 FlgNet 再经 Openness XML 导入，请改用 write_lad_block(kind="OB")。'
        ))
    try:
        name, source = render_lad_block(spec)
        destination = output_path(output_directory, "output_directory")
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / f"{name}.s7dcl"
        if path.exists() and path.read_text(encoding="utf-8-sig") != source:
            raise FileExistsError(f"目标文件已存在且内容不同：{path}")
        path.write_text(source, encoding="utf-8-sig", newline="\n")
    except Exception as error:
        return _error_result(error)
    result: dict[str, Any] = {
        "ok": True,
        "block": name,
        "format": "SIMATIC SD",
        "path": str(path),
        "sha256": hashlib.sha256(source.encode("utf-8-sig")).hexdigest(),
        "network_count": len(spec["networks"]),
        "target_version": version.api_version,
    }
    if include_source:
        result["source"] = source
    return result


async def write_lad_block(
    spec: dict[str, Any],
    target_folder_path: str | None = None,
    plc_name: str | None = None,
    allow_overwrite: bool = False,
    compile_after: bool = True,
    confirm_write: bool = False,
) -> dict[str, Any]:
    """生成并导入 LAD 块；未指定 kind/name 时按控制台块偏好补齐，并补中文网络注释。

    必须离线且 confirm_write=true；默认编译软件但不保存、下载。
    同名块需 allow_overwrite=true；全局语言偏好不会覆盖对本 LAD 工具的显式调用。
    """
    try:
        spec, applied_policy = _apply_default_lad_policy(spec)
    except Exception as error:
        return _error_result(error)
    version_result = await _tia_call(service.require_simatic_sd_lad)
    if isinstance(version_result, dict) and version_result.get("ok") is False:
        version_result["programming_policy"] = applied_policy
        return version_result
    target_version = version_result
    if str(spec.get("kind", "OB")).upper() == "OB":
        result = await _write_lad_ob(
            spec,
            target_folder_path,
            plc_name,
            allow_overwrite,
            compile_after,
            confirm_write,
        )
        result["programming_policy"] = applied_policy
        return result
    try:
        name, source = render_lad_block(spec)
    except Exception as error:
        return _error_result(error)
    result = await _tia_call(
        service.write_program_blocks,
        source,
        f"{name}.s7dcl",
        target_folder_path,
        plc_name,
        allow_overwrite,
        True,
        confirm_write,
    )
    result["format"] = "SIMATIC SD"
    result["target_version"] = target_version.api_version
    result["programming_policy"] = applied_policy
    if compile_after:
        compilation = await _tia_call(service.compile_plc, "software", plc_name)
        result["compile"] = compilation
        result["ok"] = result["ok"] and compilation["ok"]
        invalid = _lad_rule_errors(compilation)
        if invalid:
            # 非法 LAD 网络留在工程里，TIA 一渲染就会崩，立即删块兜底。
            cleanup = await _tia_call(
                service.delete_program_blocks,
                [name],
                plc_name,
                False,
                None,
                True,
            )
            result["ok"] = False
            result["invalid_lad_networks"] = invalid
            result["reverted"] = True
            result["revert_reason"] = (
                f"LAD 网络不符合 LAD 规则，已删除块 {name}，"
                "以免 TIA 渲染该网络时崩溃。"
            )
            result["reverted_source_sha256"] = hashlib.sha256(
                source.encode("utf-8-sig")
            ).hexdigest()
            result["reverted_source"] = source
            result["cleanup"] = cleanup
    return result


def _exported_xml(export_result: Any, directory: Path) -> str | None:
    """从导出结果里找出 XML 文件并读出内容。"""
    if not isinstance(export_result, dict) or not export_result.get("ok"):
        return None
    candidates = [
        Path(str(path))
        for path in (export_result.get("created_files") or [])
        if str(path).casefold().endswith(".xml")
    ]
    if not candidates:
        candidates = sorted(directory.glob("*.xml"))
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8-sig")
    return None


async def _restore_ob(
    name: str,
    original_xml: str | None,
    plc_name: str | None,
    created_new: bool,
) -> dict[str, Any]:
    """回滚目标 OB：原本存在则原样导回，本次新建则删除。"""
    if not created_new and original_xml:
        with tempfile.TemporaryDirectory(prefix="tia_mcp_ob_revert_") as temp:
            target = Path(temp) / f"{name}.xml"
            target.write_text(original_xml, encoding="utf-8-sig", newline="\n")
            restored = await _tia_call(
                service.import_program_blocks, str(target), None, plc_name, True
            )
            return {"action": "已还原原 OB", "detail": restored}
    removed = await _tia_call(
        service.delete_program_blocks, [name], plc_name, False, None, True
    )
    return {"action": "已删除本次新建的 OB", "detail": removed}


async def _write_lad_ob(
    spec: dict[str, Any],
    target_folder_path: str | None,
    plc_name: str | None,
    allow_overwrite: bool,
    compile_after: bool,
    confirm_write: bool,
) -> dict[str, Any]:
    """用 LAD 写入或覆盖一个 OB（如 Main(OB1)）。

    OB 不能像 FB/FC 那样直接用 SIMATIC SD 生成：SD 的块属性对 OB 不生效，
    S7-1200 G2 的 OB 会因缺少「优化块访问」而编译失败。因此这里走已验证的路径：
    先造一个临时 LAD FC，让 **TIA 自己生成权威 FlgNet**，再把该 FlgNet 套进目标 OB
    的 XML 骨架（保留其 Number/SecondaryType 等属性）导入覆盖。

    安全设计：
    - 目标 OB 已存在且 allow_overwrite=false 时先中止，不触碰它；
    - 覆盖前先导出原 OB，编译若报 LAD 规则错误则**原样还原**（OB1 不能一删了之）；
    - 临时参考块在 finally 中删除，任何分支都不会留在工程里。
    """
    if not confirm_write:
        return _error_result(ValueError("写入程序块必须设置 confirm_write=true。"))

    name = spec.get("name")
    if not isinstance(name, str) or not name.strip():
        return _error_result(ValueError("spec.name 不能为空。"))
    name = name.strip()
    if spec.get("interface"):
        return _error_result(ValueError(
            "OB 没有用户接口（只有 Initial_Call / Remanence），spec 里不要写 interface；"
            "需要局部量请放到被它调用的 FB/FC 中。"
        ))
    networks = spec.get("networks")
    if not isinstance(networks, list) or not networks:
        return _error_result(ValueError("networks 必须是非空数组。"))

    # 规格里「调 FB 却没给 instance」是最常见的失败原因：临时参考块是 FC，
    # FC 里调 FB 且没有实例 DB，TIA 编译必报缺实例 DB。这里先给出可读提示，
    # 不阻断写入（目标块的类型无法从这里确定），失败时再据此给出结论。
    call_warnings = _calls_without_instance(networks)

    result: dict[str, Any] = {
        "ok": False,
        "block": name,
        "block_type": "OB",
        "format": "Openness XML",
    }
    if call_warnings:
        result["call_warnings"] = call_warnings

    # ---- 0) 先读块清单，确认目标 OB 与覆盖授权：这一步没有任何副作用 ----
    inventory = await _tia_call(service.plc_inventory, plc_name, False, False)
    existing_names = [
        str(item.get("name", ""))
        for item in (inventory or {}).get("program_blocks", [])
    ]
    exists = any(item.casefold() == name.casefold() for item in existing_names)
    if exists and not allow_overwrite:
        result["stage"] = "目标 OB 已存在"
        result["reason"] = f"程序块 {name} 已存在；如需覆盖请设置 allow_overwrite=true。"
        result["existing_blocks"] = existing_names
        return result

    temp_name = f"MCP_LadObTemp_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    original_xml: str | None = None
    created_new = False

    with tempfile.TemporaryDirectory(prefix="tia_mcp_ob_") as temp:
        temp_dir = Path(temp)
        reference_created = False
        try:
            # ---- 1) 临时 LAD FC：让 TIA 生成权威 FlgNet ----
            try:
                _, source = render_lad_block(
                    {"name": temp_name, "kind": "FC", "networks": networks}
                )
            except Exception as error:
                return _error_result(error)

            written = await _tia_call(
                service.write_program_blocks,
                source,
                f"{temp_name}.s7dcl",
                None,
                plc_name,
                False,
                True,
                True,
            )
            if not isinstance(written, dict) or not written.get("ok"):
                result.update({
                    "stage": "创建 LAD 参考块失败",
                    "reason": "规格本身可能不合法；未触碰目标 OB。",
                    "detail": written,
                })
                return result
            reference_created = True

            # 新写入的块在编译前处于「不一致」状态，Openness 会拒绝导出：
            #   Inconsistent blocks and PLC data types (UDT) cannot be exported.
            # 必须先编译，才能取出 TIA 生成的权威 FlgNet。
            reference_compile = await _tia_call(service.compile_plc, "software", plc_name)
            if not isinstance(reference_compile, dict) or not reference_compile.get("ok"):
                spec_errors, project_errors = _split_compile_errors(
                    reference_compile, temp_name
                )
                result.update({
                    "stage": "参考块编译失败",
                    "reason": "规格生成的 LAD 网络未通过编译；未触碰目标 OB。",
                    "reference_compile": reference_compile,
                    "spec_errors": spec_errors,
                    "project_errors": project_errors,
                    "temp_block": temp_name,
                })
                # 删掉临时块再编译一次：区分「规格写错」和「工程本来就不干净」。
                # 这是只读诊断，目标 OB 全程没有被触碰。
                cleanup = await _tia_call(
                    service.delete_program_blocks,
                    [temp_name],
                    plc_name,
                    False,
                    None,
                    True,
                )
                reference_created = False
                result["reference_cleanup"] = (
                    "已删除临时参考块"
                    if isinstance(cleanup, dict) and cleanup.get("ok")
                    else f"临时参考块 {temp_name} 未能删除，请手动处理"
                )
                baseline = await _tia_call(service.compile_plc, "software", plc_name)
                baseline_clean = bool(isinstance(baseline, dict) and baseline.get("ok"))
                result["baseline_compile"] = baseline
                result["baseline_clean"] = baseline_clean
                if baseline_clean:
                    result["diagnosis"] = (
                        "工程基线编译正常：失败由本次 LAD 规格生成的内容引起。"
                    )
                    result["next_steps"] = _reference_failure_steps(
                        call_warnings, spec_errors
                    )
                else:
                    result["diagnosis"] = (
                        "工程里本来就有编译错误（删掉临时参考块后依然失败），"
                        "与本次 LAD 规格无关；请先修好这些错误再写 OB。"
                    )
                    result["project_errors"] = (
                        _compile_messages(baseline) or project_errors
                    )
                    result["next_steps"] = [
                        '先调用 compile_plc(scope="software") 查看并修复既有错误，'
                        '再重新调用 write_lad_block(kind="OB")。'
                    ]
                return result


            exported = await _tia_call(
                service.export_program_blocks,
                str(temp_dir),
                [temp_name],
                "simaticml",
                plc_name,
            )
            reference_xml = _exported_xml(exported, temp_dir)
            if reference_xml is None:
                result.update({"stage": "导出参考块失败", "detail": exported})
                return result
            try:
                flgnet = extract_flgnet(reference_xml)
            except Exception as error:
                result.update(_error_result(error))
                result["stage"] = "参考块里没有可用的 FlgNet"
                return result

            # ---- 2) 目标 OB 是否已存在：清单已在步骤 0 读取 ----
            if exists:
                backup_dir = temp_dir / "original_ob"
                backup_dir.mkdir(exist_ok=True)
                original_export = await _tia_call(
                    service.export_program_blocks,
                    str(backup_dir),
                    [name],
                    "simaticml",
                    plc_name,
                )
                original_xml = _exported_xml(original_export, backup_dir)
                if original_xml is None:
                    result.update({
                        "stage": "导出原 OB 失败",
                        "reason": "无法保证可回滚，故中止，未做任何修改。",
                        "detail": original_export,
                    })
                    return result
                skeleton = original_xml
            else:
                connected_version = getattr(service, "connected_version", None) or "21.0"
                skeleton = default_ob_skeleton(connected_version)
                created_new = True

            # ---- 3) 组装 OB XML 并导入 ----
            xml = build_lad_ob_xml(skeleton, flgnet, name, networks)
            assert_lad_ob_xml(xml, name)
            target_xml = temp_dir / f"{name}.xml"
            target_xml.write_text(xml, encoding="utf-8-sig", newline="\n")

            imported = await _tia_call(
                service.import_program_blocks,
                str(target_xml),
                target_folder_path,
                plc_name,
                True,
            )
            if not isinstance(imported, dict) or not imported.get("ok"):
                result.update({"stage": "导入 OB 失败", "detail": imported})
                return result

            result.update({
                "ok": True,
                "new_blocks": [] if exists else [name],
                "overwritten_blocks": [name] if exists else [],
                "network_count": len(flgnet),
                "created_new_ob": created_new,
                "preserved_overwrite_source": exists,
            })
        finally:
            if reference_created:
                cleanup = await _tia_call(
                    service.delete_program_blocks,
                    [temp_name],
                    plc_name,
                    False,
                    None,
                    True,
                )
                result["reference_cleanup"] = (
                    "已删除临时参考块"
                    if isinstance(cleanup, dict) and cleanup.get("ok")
                    else f"临时参考块 {temp_name} 未能删除，请手动处理"
                )

    if result.get("ok") and compile_after:
        compilation = await _tia_call(service.compile_plc, "software", plc_name)
        result["compile"] = compilation
        invalid = _lad_rule_errors(compilation)
        result["ok"] = bool(isinstance(compilation, dict) and compilation.get("ok"))
        if not result["ok"] and not invalid:
            # OB 已经写进工程，但编译还有别的错误：把可执行的诊断一并返回。
            result["stage"] = "OB 已写入但编译未通过"
            result["compile_errors"] = _compile_messages(compilation)
            result["diagnosis"] = (
                "目标 OB 已写入，编译报错来自操作数/变量表或其它块；"
                "按 compile_errors 修好后重新编译即可，无需重写 OB。"
            )
        if invalid:
            result["ok"] = False
            result["invalid_lad_networks"] = invalid
            result["reverted"] = await _restore_ob(
                name, original_xml, plc_name, created_new
            )
            result["revert_reason"] = (
                "LAD 网络不符合 LAD 规则，已回滚目标 OB，"
                "以免 TIA 渲染该网络时崩溃。"
            )
    return result


async def import_program_blocks(
    source_path: str,
    target_folder_path: str | None = None,
    plc_name: str | None = None,
    confirm_import: bool = False,
) -> dict[str, Any]:
    """从单个文件或隔离目录导入程序块，必须 confirm_import=true。"""
    return await _tia_call(
        service.import_program_blocks,
        source_path,
        target_folder_path,
        plc_name,
        confirm_import,
    )


async def export_plc_tag_tables(
    target_directory: str,
    table_names: list[str] | None = None,
    plc_name: str | None = None,
) -> dict[str, Any]:
    """导出 PLC 变量表；table_names=null 表示全部。"""
    return await _tia_call(
        service.export_plc_tag_tables, target_directory, table_names, plc_name
    )


async def import_plc_tag_tables(
    source_path: str,
    target_folder_path: str | None = None,
    plc_name: str | None = None,
    confirm_import: bool = False,
) -> dict[str, Any]:
    """从文件或目录导入 PLC 变量表，必须 confirm_import=true。"""
    return await _tia_call(
        service.import_plc_tag_tables,
        source_path,
        target_folder_path,
        plc_name,
        confirm_import,
    )


async def export_udts(
    target_directory: str,
    udt_names: list[str] | None = None,
    plc_name: str | None = None,
) -> dict[str, Any]:
    """导出 PLC 用户数据类型（UDT）；udt_names=null 表示全部。"""
    return await _tia_call(service.export_udts, target_directory, udt_names, plc_name)


async def import_udts(
    source_path: str,
    target_folder_path: str | None = None,
    plc_name: str | None = None,
    confirm_import: bool = False,
) -> dict[str, Any]:
    """从文件或目录导入 UDT，必须 confirm_import=true。"""
    return await _tia_call(
        service.import_udts,
        source_path,
        target_folder_path,
        plc_name,
        confirm_import,
    )


async def export_watch_tables(
    target_directory: str,
    table_names: list[str] | None = None,
    plc_name: str | None = None,
) -> dict[str, Any]:
    """导出监控表；table_names=null 表示全部。"""
    return await _tia_call(
        service.export_watch_tables, target_directory, table_names, plc_name
    )


async def import_watch_tables(
    source_path: str,
    target_folder_path: str | None = None,
    plc_name: str | None = None,
    confirm_import: bool = False,
) -> dict[str, Any]:
    """从文件或目录导入监控表，必须 confirm_import=true。"""
    return await _tia_call(
        service.import_watch_tables,
        source_path,
        target_folder_path,
        plc_name,
        confirm_import,
    )


async def get_technology_object_details(
    object_names: list[str] | None = None,
    plc_name: str | None = None,
    include_properties: bool = True,
    include_fingerprints: bool = False,
) -> dict[str, Any]:
    """读取工艺对象的属性、路径、一致性和支持的导出格式。"""
    return await _tia_call(
        service.get_technology_object_details,
        object_names,
        plc_name,
        include_properties,
        include_fingerprints,
    )


async def export_technology_objects(
    target_directory: str,
    object_names: list[str] | None = None,
    plc_name: str | None = None,
) -> dict[str, Any]:
    """把工艺对象导出为同版本 SimaticML 模板；object_names=null 表示全部。"""
    return await _tia_call(
        service.export_technology_objects,
        target_directory,
        object_names,
        plc_name,
    )


async def import_technology_objects(
    source_path: str,
    target_folder_path: str | None = None,
    plc_name: str | None = None,
    confirm_import: bool = False,
) -> dict[str, Any]:
    """从同版本 SimaticML 模板创建或覆盖工艺对象，必须 confirm_import=true。"""
    return await _tia_call(
        service.import_technology_objects,
        source_path,
        target_folder_path,
        plc_name,
        confirm_import,
    )


async def set_technology_object_properties(
    object_name: str,
    properties: dict[str, Any],
    plc_name: str | None = None,
    verify_readback: bool = True,
    compile_after: bool = True,
    rollback_on_error: bool = True,
    confirm_write: bool = False,
) -> dict[str, Any]:
    """事务式修改工艺对象已公开属性；默认回读、编译，失败时回滚。"""
    return await _tia_call(
        service.set_technology_object_properties,
        object_name,
        properties,
        plc_name,
        verify_readback,
        compile_after,
        rollback_on_error,
        confirm_write,
    )


async def compile_technology_objects(
    object_names: list[str] | None = None,
    plc_name: str | None = None,
) -> dict[str, Any]:
    """逐个编译工艺对象；object_names=null 表示全部。"""
    return await _tia_call(
        service.compile_technology_objects,
        object_names,
        plc_name,
    )


async def delete_technology_objects(
    object_names: list[str],
    plc_name: str | None = None,
    backup_before_delete: bool = True,
    backup_directory: str | None = None,
    confirm_delete: bool = False,
) -> dict[str, Any]:
    """删除工艺对象；默认先备份到显式目录，且必须 confirm_delete=true。"""
    return await _tia_call(
        service.delete_technology_objects,
        object_names,
        plc_name,
        backup_before_delete,
        backup_directory,
        confirm_delete,
    )


async def compile_plc(
    scope: str = "both", plc_name: str | None = None
) -> dict[str, Any]:
    """编译 PLC 硬件、软件或两者；scope=hardware/software/both。"""
    return await _tia_call(service.compile_plc, scope, plc_name)


async def download_to_plc(
    pc_interface_type: str,
    pc_interface_name: str,
    target_interface: str,
    plc_name: str | None = None,
    download_mode: str = "software-changes",
    compile_before_download: bool = True,
    allow_physical_plc: bool = False,
    allow_cpu_stop: bool = False,
    allow_db_reinitialization: bool = False,
    allow_target_difference: bool = False,
    start_cpu_after_download: bool = False,
    confirm_download: bool = False,
    plc_master_secret_password: str | None = None,
    module_read_password: str | None = None,
    module_write_password: str | None = None,
) -> dict[str, Any]:
    """编译并下载 PLC；实体 PLC 及高风险选项均需单独明确授权。

    S7-1200 G2 若启用了机密 PLC 组态数据/模块访问保护，可通过
    plc_master_secret_password / module_read_password / module_write_password
    传入密码；不传时不会用空密码覆盖。
    """
    return await _tia_call(
        service.download_to_plc,
        pc_interface_type,
        pc_interface_name,
        target_interface,
        plc_name,
        download_mode,
        compile_before_download,
        allow_physical_plc,
        allow_cpu_stop,
        allow_db_reinitialization,
        allow_target_difference,
        start_cpu_after_download,
        confirm_download,
        plc_master_secret_password,
        module_read_password,
        module_write_password,
    )


async def save_project(confirm_save: bool = False) -> dict[str, Any]:
    """保存当前 TIA 工程，必须 confirm_save=true。"""
    return await _tia_call(service.save_project, confirm_save)


async def archive_project(
    target_directory: str,
    archive_name: str,
    overwrite: bool = False,
    confirm_archive: bool = False,
) -> dict[str, Any]:
    """归档当前工程；覆盖同名归档由 overwrite 控制。"""
    return await _tia_call(
        service.archive_project,
        target_directory,
        archive_name,
        overwrite,
        confirm_archive,
    )


async def backup_plc_software(
    target_directory: str,
    plc_name: str | None = None,
) -> dict[str, Any]:
    """备份程序块、变量表、UDT 和监控表到调用方指定目录。"""
    return await _tia_call(service.backup_plc_software, target_directory, plc_name)


async def compare_plc_to_online(plc_name: str | None = None) -> dict[str, Any]:
    """比较离线工程与在线 PLC。需要已具备可用的在线连接。"""
    return await _tia_call(service.compare_to_online, plc_name)


async def update_module_descriptions(
    scope: str = "plc", plc_name: str | None = None
) -> dict[str, Any]:
    """刷新模块描述（硬件目录更新后修复「模块不可用」）。scope=project/plc。"""
    return await _tia_call(extras.update_module_descriptions, service, scope, plc_name)


async def export_project_texts(
    target_file: str,
    source_language: str | None = None,
    target_language: str | None = None,
) -> dict[str, Any]:
    """导出工程多语言文本（注释/标题/画面文本），用于翻译与评审。"""
    return await _tia_call(
        extras.export_project_texts,
        service,
        target_file,
        source_language,
        target_language,
    )


async def import_project_texts(
    source_file: str,
    update_source_language: bool = False,
    confirm_import: bool = False,
) -> dict[str, Any]:
    """把翻译后的工程文本导回工程；必须 confirm_import=true。"""
    return await _tia_call(
        extras.import_project_texts,
        service,
        source_file,
        update_source_language,
        confirm_import,
    )


async def save_project_as(
    target_directory: str,
    project_name: str,
    overwrite: bool = False,
    confirm_save_as: bool = False,
) -> dict[str, Any]:
    """把当前工程另存为新工程（副本）；必须 confirm_save_as=true。"""
    return await _tia_call(
        extras.save_project_as,
        service,
        target_directory,
        project_name,
        overwrite,
        confirm_save_as,
    )


async def list_libraries() -> dict[str, Any]:
    """列出全局库、项目库类型与主副本。"""
    return await _tia_call(extras.list_libraries, service)


async def create_master_copy(
    block_name: str,
    library_folder_path: str | None = None,
    plc_name: str | None = None,
    confirm_create: bool = False,
) -> dict[str, Any]:
    """把程序块存成主副本（进项目库）；必须 confirm_create=true。"""
    return await _tia_call(
        extras.create_master_copy,
        service,
        block_name,
        library_folder_path,
        plc_name,
        confirm_create,
    )


async def create_global_library(
    target_directory: str,
    library_name: str,
    overwrite: bool = False,
    confirm_create: bool = False,
) -> dict[str, Any]:
    """新建全局库（.al21）用于跨工程复用；必须 confirm_create=true。"""
    return await _tia_call(
        extras.create_global_library,
        service,
        target_directory,
        library_name,
        overwrite,
        confirm_create,
    )


async def open_global_library(
    library_path: str, confirm_open: bool = False
) -> dict[str, Any]:
    """打开已有全局库；必须 confirm_open=true。"""
    return await _tia_call(
        extras.open_global_library, service, library_path, confirm_open
    )


async def export_cross_references(
    target_directory: str,
    block_names: list[str] | None = None,
    cross_reference_filter: int = 0,
    plc_name: str | None = None,
) -> dict[str, Any]:
    """导出程序块交叉引用（调用关系与全局符号使用）。"""
    return await _tia_call(
        extras.export_cross_references,
        service,
        target_directory,
        block_names,
        cross_reference_filter,
        plc_name,
    )


async def set_block_protection(
    block_name: str,
    action: str = "set",
    password: str | None = None,
    plc_name: str | None = None,
    confirm_change: bool = False,
) -> dict[str, Any]:
    """给程序块加/解除专有技术保护；必须 confirm_change=true。"""
    return await _tia_call(
        extras.set_block_protection,
        service,
        block_name,
        action,
        password,
        plc_name,
        confirm_change,
    )


async def read_hsc_configuration(plc_name: str | None = None) -> dict[str, Any]:
    """读取已人工组态的高速计数器：硬件标识、计数值符号，以及读不到的项。"""
    return await _tia_call(extras.read_hsc_configuration, service, plc_name)


async def get_plc_security_settings(plc_name: str | None = None) -> dict[str, Any]:
    """读取 PLC「防护与安全」中 Openness 实际暴露的属性。

    未暴露的页面（机密组态数据、访问等级、用户管理、证书管理器）会在 pages 中标注
    manual_steps；不要在没读到属性时猜属性名。
    """
    return await _tia_call(plc_security.get_security_settings, service, plc_name)


async def set_plc_security_settings(
    settings: dict[str, Any],
    plc_name: str | None = None,
    verify_readback: bool = True,
    compile_after: bool = True,
    rollback_on_error: bool = True,
    confirm_write: bool = False,
) -> dict[str, Any]:
    """写入白名单内的 PLC 安全属性，必须 confirm_write=true。

    支持 access_control、put_get_communication、web_server、
    syslog_auto_accept_client、security_events_*；写入后需编译硬件并下载才生效。
    机密组态密码、访问等级、用户/角色、证书管理没有 Openness API，需在 TIA 中人工配置。
    """
    return await _tia_call(
        plc_security.set_security_settings,
        service,
        settings,
        plc_name,
        verify_readback,
        compile_after,
        rollback_on_error,
        confirm_write,
    )


async def get_plc_security_guide() -> dict[str, Any]:
    """返回 PLC 安全页面与 Openness 可达性对照，以及必须人工处理的步骤。"""
    return await _tia_call(plc_security.security_guide)


async def create_program_block_group(
    group_path: str,
    plc_name: str | None = None,
    confirm_create: bool = False,
) -> dict[str, Any]:
    """创建程序块分组（目录）并命名，支持 A/B 多级；必须离线并 confirm_create=true。

    已存在的分组会幂等返回 already_exists=true。空分组可保留；创建后不会自动保存。
    """
    return await _tia_call(
        plc_groups.create_program_block_group,
        service,
        group_path,
        plc_name,
        confirm_create,
    )


def register_tools(mcp: MCPServer) -> None:
    """将全部 TIA MCP 接口注册到启动入口创建的服务实例。"""
    tool_definitions = (
        (get_service_info, READ_ONLY),
        (list_installed_tia_products, READ_ONLY),
        (check_openness_prerequisites, READ_ONLY),
        (get_openness_access_guide, READ_ONLY),
        (list_running_tia_portals, READ_ONLY),
        (get_project_creation_capabilities, READ_ONLY),
        (create_tia_project, DESTRUCTIVE),
        (detect_tia_installations, READ_ONLY),
        (launch_tia, DESTRUCTIVE),
        (export_project_aml, WRITE),
        (add_device_from_aml, DESTRUCTIVE),
        (add_device_from_template, DESTRUCTIVE),
        (list_device_templates, READ_ONLY),
        (get_default_programming_policy, READ_ONLY),
        (get_output_template, READ_ONLY),
        (get_knowledge_base_status, READ_ONLY),
        (list_knowledge_documents, READ_ONLY),
        (search_knowledge_base, READ_ONLY),
        (read_knowledge_document, READ_ONLY),
        (connect_tia, WRITE),
        (disconnect_tia, DESTRUCTIVE),
        (get_tia_status, READ_ONLY),
        (get_tia_compatibility, READ_ONLY),
        (select_plc, WRITE),
        (get_device_configuration, READ_ONLY),
        (get_hardware_inventory, READ_ONLY),
        (find_hardware_property, READ_ONLY),
        (set_hardware_properties, DESTRUCTIVE),
        (configure_s71200_hsc, DESTRUCTIVE),
        (get_plc_inventory, READ_ONLY),
        (list_windows_network_adapters, READ_ONLY),
        (go_online, OPEN_WORLD),
        (go_offline, WRITE),
        (export_program_blocks, READ_ONLY),
        (delete_program_blocks, DESTRUCTIVE),
        (write_program_blocks, DESTRUCTIVE),
        (prepare_lad_block, WRITE),
        (write_lad_block, DESTRUCTIVE),
        (import_program_blocks, DESTRUCTIVE),
        (export_plc_tag_tables, READ_ONLY),
        (import_plc_tag_tables, DESTRUCTIVE),
        (export_udts, READ_ONLY),
        (import_udts, DESTRUCTIVE),
        (export_watch_tables, READ_ONLY),
        (import_watch_tables, DESTRUCTIVE),
        (get_technology_object_details, READ_ONLY),
        (export_technology_objects, READ_ONLY),
        (import_technology_objects, DESTRUCTIVE),
        (set_technology_object_properties, DESTRUCTIVE),
        (compile_technology_objects, WRITE),
        (delete_technology_objects, DESTRUCTIVE),
        (compile_plc, WRITE),
        (download_to_plc, DESTRUCTIVE),
        (save_project, DESTRUCTIVE),
        (archive_project, WRITE),
        (backup_plc_software, READ_ONLY),
        (compare_plc_to_online, READ_ONLY),
        (update_module_descriptions, WRITE),
        (export_project_texts, READ_ONLY),
        (import_project_texts, WRITE),
        (save_project_as, WRITE),
        (list_libraries, READ_ONLY),
        (create_master_copy, WRITE),
        (create_global_library, WRITE),
        (open_global_library, WRITE),
        (export_cross_references, READ_ONLY),
        (set_block_protection, DESTRUCTIVE),
        (read_hsc_configuration, READ_ONLY),
        (get_plc_security_settings, READ_ONLY),
        (set_plc_security_settings, DESTRUCTIVE),
        (get_plc_security_guide, READ_ONLY),
        (create_program_block_group, WRITE),
    )
    for function, annotations in tool_definitions:
        _TOOL_REGISTRY[function.__name__] = function
        if function.__name__ in POLICY_HINT_TOOLS:
            # 把控制台偏好写进工具描述：模型每次拉取工具列表都能看到，
            # 不依赖客户端是否转达 initialize 的 instructions。
            hint = programming_hint()
            function.__doc__ = f"{hint}\n\n{function.__doc__ or ''}".strip()
        if function.__name__ in TEMPLATE_HINT_TOOLS:
            # 输出模板同理：连接类工具的描述里直接带上模板要求。
            hint = output_template_hint()
            function.__doc__ = f"{hint}\n\n{function.__doc__ or ''}".strip()
        if function.__name__ in KNOWLEDGE_HINT_TOOLS:
            # 知识库路径和可用状态也写进相关工具描述；正文仍由只读工具按需返回。
            hint = local_knowledge.knowledge_base_hint()
            function.__doc__ = f"{hint}\n\n{function.__doc__ or ''}".strip()
        mcp.tool(annotations=annotations)(function)
