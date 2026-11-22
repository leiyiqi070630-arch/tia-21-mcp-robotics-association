"""PLC「防护与安全」配置的 Openness 可达性封装。

TIA 的 PLC 安全设置里有相当一部分只存在于界面（机密组态数据密码、访问等级、
用户管理、证书管理器），Openness 没有对应 API；本模块把**实际暴露**的动态属性
整理成可读写工具，并把不可达页面转成明确的人工步骤，避免 AI 猜测属性名。
"""

from __future__ import annotations

from typing import Any

from core.tia_service import _safe_properties


# 每个条目：友好键 -> Openness 动态属性名与校验规则。
SECURITY_PROPERTIES: dict[str, dict[str, Any]] = {
    "access_control": {
        "property": "PlcAccessControlConfiguration",
        "type": "enum",
        "choices": ("EnabledWithAccessControl", "Disabled"),
        "writable": True,
        "page": "访问控制",
        "description": "PLC 访问控制（UMAC/访问等级）总开关。",
    },
    "put_get_communication": {
        "property": "ProtectionEnablePutGetCommunication",
        "type": "bool",
        "writable": True,
        "page": "连接机制",
        "description": "允许来自远程对象的 PUT/GET 通信访问。",
    },
    "web_server": {
        "property": "WebserverActivate",
        "type": "bool",
        "writable": True,
        "page": "Web 服务器",
        "description": "启用 PLC Web 服务器。",
    },
    "syslog_auto_accept_client": {
        "property": "SysLogAutoAcceptClient",
        "type": "bool",
        "writable": True,
        "page": "Syslog",
        "description": "自动接受 Syslog 客户端。",
    },
    "security_events_interval": {
        "property": "ProtectionIntervalForSummarizeOfSecurityEvents",
        "type": "int",
        "minimum": 1,
        "writable": True,
        "page": "安全事件",
        "description": "安全事件汇总间隔（分钟，具体单位以 TIA 为准）。",
    },
    "security_events_high_load": {
        "property": "ProtectionSummarizeSecurityEventsOnHighLoad",
        "type": "bool",
        "writable": True,
        "page": "安全事件",
        "description": "高负载时仍汇总安全事件。",
    },
    "security_events_unit": {
        "property": "ProtectionUnitForSummarizeOfSecurityEvents",
        "type": "int",
        "minimum": 1,
        "writable": True,
        "page": "安全事件",
        "description": "安全事件汇总单位。",
    },
    "communication_mode": {
        "property": "CommunicationMode",
        "type": "str",
        "writable": False,
        "page": "与 TIA Portal 和 HMI 的通信模式",
        "description": "与 TIA Portal/HMI 的通信模式（枚举未公开，只读）。",
    },
    "syslog_client_certificate_id": {
        "property": "SysLogClientCertificateId",
        "type": "str",
        "writable": False,
        "page": "Syslog",
        "description": "Syslog 客户端证书 ID（证书管理需在 TIA 中完成）。",
    },
    "syslog_trusted_certificate_ids": {
        "property": "SysLogTrustedCertificateIds",
        "type": "str",
        "writable": False,
        "page": "Syslog",
        "description": "Syslog 受信任证书 ID 列表（只读/需证书管理）。",
    },
}


def _page(
    name: str,
    *,
    read: bool,
    write: bool,
    detail: str,
    steps: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "page": name,
        "openness_read": read,
        "openness_write": write,
        "detail": detail,
        "manual_steps": steps or [],
    }


def security_pages() -> list[dict[str, Any]]:
    """TIA「防护与安全」页面与 Openness 可达性的固定对照表。"""
    return [
        _page(
            "保护机密的 PLC 组态数据",
            read=False,
            write=False,
            detail="密码与「下载组态数据的附加保护」只能由用户在 TIA 的 PLC 安全设置中输入。",
            steps=[
                "在 TIA 设备视图中选中 PLC，打开「属性」→「防护与安全」→「保护 PLC 组态数据」。",
                "勾选「保护机密的 PLC 组态数据」，点击「设置」输入密码。",
                "需要下载附加保护时，同时勾选「可下载组态数据的附加保护」。",
                "完成后重新编译硬件/下载；调用方也可在 download_to_plc 中传 plc_master_secret_password。",
            ],
        ),
        _page(
            "访问控制 / 访问等级 / 用户管理",
            read=False,
            write=False,
            detail="Openness 只暴露 PlcAccessControlConfiguration 总开关，不暴露用户、角色、访问等级表。",
            steps=[
                "在 TIA 中打开 PLC「防护与安全」→「访问控制」/「用户管理组态」。",
                "按需配置用户、角色、访问等级和匿名访问。",
                "若要整体启用/停用访问控制，可调用 set_plc_security_settings(access_control=...)。",
            ],
        ),
        _page(
            "证书管理器",
            read=False,
            write=False,
            detail="Openness 没有证书导入/信任 API；证书只能通过 TIA 证书管理器或首次转在线信任。",
            steps=[
                "在 TIA 中打开「防护与安全」→「证书管理器」→「使用 TIA Portal 进行证书管理」。",
                "导入/信任 PLC 证书；首次转在线时也可在提示框选择「信任」。",
                "完成后再调用 go_online / download_to_plc。",
            ],
        ),
        _page(
            "与 TIA Portal 和 HMI 的通信模式",
            read=True,
            write=False,
            detail="只读 CommunicationMode；可选值的枚举没有在桥接层公开。",
            steps=[
                "需要修改时，在 TIA 的「防护与安全」→「连接机制」中调整。",
            ],
        ),
        _page(
            "允许来自远程对象的 PUT/GET 通信访问",
            read=True,
            write=True,
            detail="对应 ProtectionEnablePutGetCommunication，可直接读写。",
        ),
        _page(
            "安全事件",
            read=True,
            write=True,
            detail="汇总间隔、高负载汇总、汇总单位三个属性可直接读写；更细的事件项未暴露。",
        ),
        _page(
            "Syslog 服务器 / Syslog 证书",
            read=True,
            write=False,
            detail="只暴露 SysLogAutoAcceptClient 等标量；服务器列表和证书管理需在 TIA 中完成。",
            steps=[
                "在 TIA 的「防护与安全」→「Syslog」中配置服务器和证书。",
                "如只需自动接受客户端，可调用 set_plc_security_settings(syslog_auto_accept_client=true)。",
            ],
        ),
        _page(
            "Web 服务器",
            read=True,
            write=True,
            detail="对应 WebserverActivate，可直接读写（不属于安全页但常与安全一起配置）。",
        ),
    ]


def security_guide() -> dict[str, Any]:
    """不连接 TIA 也能返回的安全能力对照表。"""
    return {
        "ok": True,
        "supported_tia": "TIA Portal V21（Openness 21.0 / 21.1）",
        "writable_settings": [
            {
                "key": key,
                "property": meta["property"],
                "type": meta["type"],
                "choices": list(meta.get("choices", [])),
                "page": meta["page"],
                "description": meta["description"],
            }
            for key, meta in SECURITY_PROPERTIES.items()
            if meta["writable"]
        ],
        "read_only_settings": [
            {
                "key": key,
                "property": meta["property"],
                "page": meta["page"],
                "description": meta["description"],
            }
            for key, meta in SECURITY_PROPERTIES.items()
            if not meta["writable"]
        ],
        "pages": security_pages(),
        "limitations": [
            "机密 PLC 组态数据密码、访问等级表、用户/角色、证书管理没有 Openness API，必须在 TIA 中人工配置。",
            "所有安全属性写入后仍需编译硬件并下载到 PLC 才会生效；纯软件下载不会应用硬件安全配置。",
            "不同 CPU/固件暴露的属性名可能不同；读取结果会标注每个属性是否存在。",
        ],
        "note": (
            "先调用 get_plc_security_settings 读取实际属性，再调用 set_plc_security_settings 写入；"
            "返回 needs_user_action 时按 user_action.steps 人工处理。"
        ),
    }


def _find_property(properties: dict[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key in properties:
        if key.casefold() == wanted:
            return key
    return None


def get_security_settings(service: Any, plc_name: str | None = None) -> dict[str, Any]:
    """读取 PLC 上实际暴露的安全相关动态属性。"""
    with service._lock:
        plc = service._require_plc(plc_name)
        properties = _safe_properties(plc)
        settings: dict[str, Any] = {}
        for key, meta in SECURITY_PROPERTIES.items():
            actual = _find_property(properties, meta["property"])
            settings[key] = {
                "property": meta["property"],
                "actual_property": actual,
                "present": actual is not None,
                "value": properties.get(actual) if actual else None,
                "writable": bool(meta["writable"]),
                "type": meta["type"],
                "page": meta["page"],
                "description": meta["description"],
            }
        raw = {
            name: value
            for name, value in properties.items()
            if any(
                marker in name.casefold()
                for marker in (
                    "protection",
                    "syslog",
                    "accesscontrol",
                    "webserver",
                    "certificate",
                    "communicationmode",
                )
            )
        }
        return {
            "ok": True,
            "plc": str(plc.get_name()),
            "settings": settings,
            "raw_security_properties": raw,
            "pages": security_pages(),
            "requires_download": True,
            "note": (
                "这些是 Openness 实际暴露的属性；未出现在 settings 的页面需在 TIA 中人工配置。"
                "写入后还需编译硬件并下载才会在 PLC 上生效。"
            ),
        }


def set_security_settings(
    service: Any,
    settings: dict[str, Any],
    plc_name: str | None = None,
    verify_readback: bool = True,
    compile_after: bool = True,
    rollback_on_error: bool = True,
    confirm_write: bool = False,
) -> dict[str, Any]:
    """写入白名单内的 PLC 安全属性（事务式、回读、可回滚）。"""
    if not confirm_write:
        raise ValueError("修改 PLC 安全设置必须设置 confirm_write=true。")
    if not isinstance(settings, dict) or not settings:
        raise ValueError("settings 必须是非空对象。")
    unknown = sorted(set(settings) - set(SECURITY_PROPERTIES))
    if unknown:
        raise ValueError(
            f"不支持的安全设置键：{unknown}；可用键："
            + ", ".join(sorted(SECURITY_PROPERTIES))
        )

    properties: dict[str, Any] = {}
    normalized: dict[str, Any] = {}
    for key, value in settings.items():
        meta = SECURITY_PROPERTIES[key]
        if not meta["writable"]:
            raise ValueError(
                f"{key} 是只读属性（{meta['property']}），需要在 TIA 中人工配置。"
            )
        expected = meta["type"]
        if expected == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{key} 必须是布尔值 true/false。")
        elif expected == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} 必须是整数。")
            minimum = meta.get("minimum")
            if minimum is not None and value < minimum:
                raise ValueError(f"{key} 不能小于 {minimum}。")
        elif expected == "enum":
            choices = meta.get("choices", ())
            if value not in choices:
                raise ValueError(f"{key} 只能是 {list(choices)} 之一。")
        properties[meta["property"]] = value
        normalized[key] = value

    plc = service._require_plc(plc_name)
    target_name = str(plc.get_name())
    result = service.set_hardware_properties(
        "plc",
        target_name,
        properties,
        plc_name=plc_name,
        verify_readback=verify_readback,
        compile_after=compile_after,
        rollback_on_error=rollback_on_error,
        confirm_write=True,
    )
    result["security_settings"] = normalized
    result["requires_download"] = True
    result["download_note"] = (
        "安全属性已写入工程；需要编译硬件并下载到 PLC 才会生效。"
        "纯软件下载不会应用这些硬件安全配置。"
    )
    return result
