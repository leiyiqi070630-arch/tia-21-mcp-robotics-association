"""把「MCP 无法自动完成、必须人工介入」的 TIA 场景转换成结构化指引。

Openness 是无界面的，TIA 里需要人点击的对话框（证书信任、机密组态密码、
Openness 防火墙授权等）无法由 MCP 代替。这里统一识别这些错误，并在工具结果里
附带 ``needs_user_action=true`` 和 ``user_action``，让 AI 停止盲目重试、把可执行
步骤原样告诉用户。
"""

from __future__ import annotations

from typing import Any, Iterable


def _guide(
    code: str,
    title: str,
    why: str,
    steps: list[str],
    retry: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "title": title,
        "why_mcp_cannot_do_it": why,
        "steps": steps,
        "retry": retry,
        "ai_instruction": (
            "不要自动重试、猜测密码或尝试绕过 TIA 的安全机制；"
            "先向用户说明 user_action.steps，等用户确认完成后再重试一次。"
        ),
    }


def explain_message(message: str) -> dict[str, Any] | None:
    """根据一条错误文本返回人工处理指引；无法识别时返回 ``None``。"""
    text = str(message or "").casefold()
    if not text:
        return None

    if "not trusted" in text or ("certificate" in text and "trust" in text):
        return _guide(
            "trust_plc_certificate",
            "TIA 需要人工信任 PLC 证书",
            "TIA 的「设备未受信任」对话框只能由用户在界面点击，Openness 没有提供信任证书的 API。",
            [
                "在 TIA 中打开当前工程（与 MCP 连接的必须是同一个工程）。",
                "菜单「在线」→「转到在线」。",
                "PG/PC 接口选择实际网卡：用 list_windows_network_adapters 返回的 "
                "InterfaceDescription（例如 MediaTek Wi-Fi 7 MT7925 Wireless LAN Card），"
                "不要用 Windows 别名（WLAN / 以太网）。",
                "目标接口填 1 X1（S7-1200 G2 的 PROFINET 接口）。",
                "出现「设备未受信任」时选择「信任 / Trust」；若同时提示机密 PLC 组态数据密码，一并输入。",
                "完成后保持在线或转离线即可，让 MCP 重新执行 go_online / download_to_plc。",
            ],
            "用户确认已信任证书后，重新调用 go_online 或 download_to_plc。",
        )

    if (
        "confidential plc configuration data" in text
        or "password for confidential" in text
        or ("password" in text and "plc configuration" in text)
    ):
        return _guide(
            "enter_plc_secret_password",
            "PLC 需要机密组态数据密码",
            "该密码只能由用户在 TIA 的 CPU 属性中输入，或调用方显式传给下载配置；MCP 不能猜测。",
            [
                "在 TIA 设备视图中打开 CPU → 属性 →「保护与安全」→「机密 PLC 组态数据」，"
                "设置或输入密码并确认。",
                "如果只是下载：可调用 download_to_plc(..., plc_master_secret_password=\"...\")；"
                "模块读写保护另用 module_read_password / module_write_password。",
                "密码需满足 TIA 的复杂度要求（长度、大小写、数字、特殊字符），以 TIA 提示为准。",
                "完成后重新编译硬件或重新下载。",
            ],
            "用户输入密码后，重新调用 compile_plc(scope=\"hardware\") 或 download_to_plc。",
        )

    if "pc interface" in text and "not found" in text and "target interface" not in text:
        return _guide(
            "select_pc_interface",
            "TIA 不认识这个 PG/PC 接口名",
            "TIA 的 PN/IE 接口名来自网卡描述，不是 Windows 连接别名；Openness 无法替用户选择接口。",
            [
                "调用 list_windows_network_adapters，取目标网卡的 InterfaceDescription 字段。",
                "把该描述原样传给 go_online / download_to_plc 的 network_adapter_name / "
                "pc_interface_name，例如 \"MediaTek Wi-Fi 7 MT7925 Wireless LAN Card\"。",
                "不要传 WLAN、以太网这类 Windows 别名。",
            ],
            "改用 InterfaceDescription 后重试。",
        )

    if "target interface" in text and "not found" in text:
        return _guide(
            "select_target_interface",
            "目标接口名不正确",
            "目标接口是 PLC 的硬件接口名，必须与 TIA「转至在线」窗口显示的一致；MCP 不能枚举它。",
            [
                "在 TIA 的「转至在线」窗口查看目标接口，S7-1200 G2 通常是 1 X1。",
                "把该值传给 target_interface，不要填 IP 地址。",
                "如果是别的 CPU/接口，以 TIA 窗口显示的名称为准。",
            ],
            "改用 TIA 窗口显示的目标接口名后重试。",
        )

    if (
        "not permitted in online mode" in text
        or ("online mode" in text and "not permitted" in text)
        or "请先调用 go_offline" in text
    ):
        return _guide(
            "already_online",
            "PLC 已经在线",
            "转在线、下载、部分组态操作要求 PLC 处于离线状态；Openness 不会替用户自动切换。",
            [
                "先调用 get_tia_status 确认 online_state。",
                "调用 go_offline 后再执行下载或组态修改。",
                "需要在线监视时，完成修改后再调用 go_online。",
            ],
            "go_offline 成功后重试原操作。",
        )

    if "连接到模块" in text or "connection to device cannot be established" in text:
        return _guide(
            "check_download_connection",
            "无法连接到目标 PLC",
            "Openness 只能报告连接失败，无法代替用户在 TIA 中排查接口、证书或设备状态。",
            [
                "先单独调用 go_online 验证在线连接是否正常。",
                "确认 network_adapter_name 使用 InterfaceDescription，target_interface 使用 1 X1。",
                "确认 PLC 证书已在 TIA 中信任（见「设备未受信任」场景）。",
                "确认 PLC 处于可下载状态、IP 可达（ping + 端口 102），且没有被其他在线会话占用。",
                "查看 TIA 的下载对话框/诊断缓冲区，确认是否有密码或安全提示。",
            ],
            "排除上述项后重新调用 download_to_plc。",
        )

    if "没有打开工程" in text or "no opened project" in text:
        return _guide(
            "open_project",
            "TIA 当前没有打开工程",
            "MCP 不会替用户关闭/新建 TIA 工程；连接前必须有一个已打开的工程。",
            [
                "在 TIA 中打开目标 .ap21 工程，或调用 create_tia_project 新建工程。",
                "确认 TIA 只有一个目标工程处于活动状态。",
                "重新调用 connect_tia。",
            ],
            "打开工程后重试 connect_tia。",
        )

    if "acknowledge the security dialog" in text or "openness" in text and "authorization" in text:
        return _guide(
            "confirm_openness_authorization",
            "需要人工确认 Openness 授权",
            "TIA 的 Openness 防火墙/UAC 授权窗口只能由用户点击，MCP 不会自动绕过。",
            [
                "查看 TIA Portal 窗口是否有 Openness 访问提示。",
                "确认程序路径无误后选择「是，全部接受 / Yes to all」。",
                "在 Windows UAC 窗口选择「是」。",
                "首次授权或 EXE 变更后都需要重新确认。",
            ],
            "完成授权后重试连接。",
        )

    return None


def format_user_message(guide: dict[str, Any]) -> str:
    """把指引拼成一段可直接展示给用户的中文说明。"""
    steps = "；".join(
        f"{index}. {step.strip().rstrip('。；;.')}"
        for index, step in enumerate(guide["steps"], 1)
    )
    return f"{guide['title']}：{steps}。"


def explain_any(messages: Iterable[Any]) -> dict[str, Any] | None:
    """从多条消息里挑出第一条能识别的人工处理指引。"""
    for message in messages:
        guide = explain_message(str(message))
        if guide is not None:
            return guide
    return None


def attach(result: dict[str, Any], *message_sources: Any) -> dict[str, Any]:
    """给工具结果附加 ``needs_user_action`` / ``user_action``（如命中）。"""
    messages: list[Any] = []
    for source in message_sources:
        if source is None:
            continue
        if isinstance(source, str):
            messages.append(source)
        elif isinstance(source, dict):
            messages.append(source.get("error", ""))
            messages.extend(source.get("errors") or [])
            messages.extend(source.get("all_messages") or [])
        else:
            messages.append(str(source))
    guide = explain_any(messages)
    if guide is not None:
        result["needs_user_action"] = True
        result["user_action"] = guide
        result["user_message"] = format_user_message(guide)
    return result
