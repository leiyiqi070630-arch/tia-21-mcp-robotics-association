from __future__ import annotations

import csv
import getpass
import hashlib
import io
import ipaddress
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator, Sequence

from core.lad_builder import render_lad_block
from core.pathing import SERVICE_DIRECTORY, absolute_path, output_path
from core import user_actions
from core.tia_versions import (
    SUPPORTED_DESCRIPTION,
    TiaVersion,
    assert_xml_import_compatible,
    available_adapter_versions,
    compatibility_matrix,
    ensure_supported,
    infer_project_version,
    is_supported_version,
    newest_version,
    normalize_requested_version,
    parse_tia_version,
    peek_tia_version,
    version_policy,
)
from core.programming_policy import programming_policy


SCL_BLOCK_PATTERN = re.compile(
    r'^\s*(?:FUNCTION_BLOCK|FUNCTION|DATA_BLOCK|ORGANIZATION_BLOCK)'
    r'\s+(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))',
    flags=re.IGNORECASE | re.MULTILINE,
)

OPENNESS_GROUP_NAME = "Siemens TIA Openness"

# siemens_tia_scripting 是单个 .pyd + 一组同级 DLL：PyInstaller 只收集 .pyd 时，
# 一旦调用到 .NET（如 get_installed_products）就抛
# FileNotFoundException，跨 CLR 边界无人处理，整个进程以 0xE0434352 退出。
# 因此调用前先确认这些程序集确实在磁盘上，缺少时给出可读错误而不是崩溃。
OPENNESS_ASSEMBLY_FILES = (
    "Siemens.TiaPortal.OpennessContracts.dll",
    "Siemens.TiaPortal.OpennessAdapter.dll",
    "Siemens.TiaPortal.OpennessApi21.dll",
    "Siemens.TiaPortal.OpennessApi21_1.dll",
    "log4net.dll",
)


def _scripting_directory() -> Path | None:
    """siemens_tia_scripting 所在目录（不触发模块导入/CLR 初始化）。"""
    module = sys.modules.get("siemens_tia_scripting")
    origin = getattr(module, "__file__", None)
    if origin:
        return Path(origin).resolve().parent
    try:
        import importlib.util

        spec = importlib.util.find_spec("siemens_tia_scripting")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).resolve().parent


def openness_assembly_directories() -> list[Path]:
    """可能存放 Openness 程序集的目录（按优先级）。"""
    candidates: list[Path] = []
    scripting_dir = _scripting_directory()
    if scripting_dir is not None:
        candidates.append(scripting_dir)
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        candidates.append(Path(bundle_dir).resolve())
    executable = Path(sys.executable).resolve()
    candidates.append(executable.parent)
    candidates.append(executable.parent / "_internal")
    return candidates


def openness_assembly_missing() -> list[str]:
    """返回缺失的 Openness 程序集文件名；全部存在时返回空列表。"""
    for directory in openness_assembly_directories():
        if all((directory / name).is_file() for name in OPENNESS_ASSEMBLY_FILES):
            return []
    return list(OPENNESS_ASSEMBLY_FILES)


# TIA Portal 主进程名（V21 的 GUI 进程）；Start.exe 是启动引导进程，单独列出便于排查。
TIA_PORTAL_PROCESS_NAMES = (
    "Siemens.Automation.Portal.exe",
    "Siemens.Automation.Portal.Start.exe",
)


def _window_titles_by_pid() -> dict[int, str]:
    """读取顶层可见窗口标题；失败时返回空字典，不影响服务运行。"""
    if os.name != "nt":
        return {}
    import ctypes
    from ctypes import wintypes

    titles: dict[int, str] = {}
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return True
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if process_id.value and title:
            titles.setdefault(int(process_id.value), title)
        return True

    try:
        user32.EnumWindows(visit, 0)
    except Exception:
        return {}
    return titles


def running_processes() -> list[tuple[int, str]]:
    """用 Toolhelp32 枚举进程（纯标准库，不依赖 tasklist/wmi，也没有编码问题）。"""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    th32cs_snapprocess = 0x00000002
    invalid_handle = ctypes.c_void_p(-1).value

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
    if not snapshot or snapshot == invalid_handle:
        return []

    entry = ProcessEntry32()
    entry.dwSize = ctypes.sizeof(ProcessEntry32)
    found: list[tuple[int, str]] = []
    try:
        if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
            return []
        while True:
            found.append(
                (
                    int(entry.th32ProcessID),
                    entry.szExeFile.decode("mbcs", errors="replace"),
                )
            )
            if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def running_tia_portals() -> list[dict[str, Any]]:
    """枚举 TIA Portal OS 进程，不依赖 Openness attach。"""
    wanted = {name.casefold() for name in TIA_PORTAL_PROCESS_NAMES}
    titles = _window_titles_by_pid()
    processes = [
        {
            "pid": pid,
            "name": name,
            "window_title": titles.get(pid, ""),
            "visible_window": pid in titles,
            "role": (
                "portal"
                if name.casefold() == "siemens.automation.portal.exe"
                else "launcher"
            ),
        }
        for pid, name in running_processes()
        if name.casefold() in wanted
    ]
    processes.sort(
        key=lambda item: (
            item["role"] != "portal",
            not item["visible_window"],
            item["pid"],
        )
    )
    for index, item in enumerate(processes):
        item["recommended"] = index == 0 and item["role"] == "portal"
    return processes


def environment_report() -> dict[str, Any]:
    """控制台主页用的环境快照：博途进程 + Openness 用户组 + 程序集完整性。"""
    portals = running_tia_portals()
    portal_processes = [item for item in portals if item.get("role") == "portal"]
    current_user = getpass.getuser()
    try:
        openness = TiaService.openness_prerequisites()
    except Exception as error:  # noqa: BLE001 - 环境探测不能影响服务
        openness = {
            "ok": False,
            "group_membership": "unknown",
            "current_user": current_user,
            "required_group": OPENNESS_GROUP_NAME,
            "message": f"检查 Openness 用户组时出错：{error}",
            "action_steps": [],
        }
    missing = openness_assembly_missing()
    membership = str(openness.get("group_membership") or "unknown")
    return {
        "ok": True,
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tia": {
            "label": "TIA Portal V21",
            "running": bool(portal_processes),
            "processes": portals,
            "recommended_process_id": next(
                (item["pid"] for item in portals if item.get("recommended")), None
            ),
        },
        "openness": {
            "ok": bool(openness.get("ok")),
            "status": membership,
            "member": membership == "member",
            "current_user": openness.get("current_user") or current_user,
            "required_group": openness.get("required_group") or OPENNESS_GROUP_NAME,
            "message": openness.get("message") or "",
            "action_steps": list(openness.get("action_steps") or []),
        },
        "assemblies": {"ready": not missing, "missing": missing},
    }

# CAx(AML) 是当前桥接层唯一能真正落地「新建设备」和「写入 IP」的通道：
# siemens_tia_scripting 既没有 CreateWithItem，也没有 NetworkInterface，
# DeviceItem/Module 的 SetAttribute 对 IPAddress 一律返回不支持（rc=20），
# 只有 export_cax_data / import_cax_data 能把硬件组态写进工程。
AML_NETWORK_ADDRESS_PATTERN = re.compile(
    r'(<Attribute\s+Name="NetworkAddress"[^>]*>\s*<Value>)([^<]*)(</Value>)',
    flags=re.IGNORECASE | re.DOTALL,
)
AML_SUBNET_MASK_PATTERN = re.compile(
    r'(<Attribute\s+Name="SubnetMask"[^>]*>\s*<Value>)([^<]*)(</Value>)',
    flags=re.IGNORECASE | re.DOTALL,
)
AML_TYPE_IDENTIFIER_PATTERN = re.compile(
    r'Name="TypeIdentifier"[^>]*>\s*<Value>([^<]*)</Value>',
    flags=re.IGNORECASE | re.DOTALL,
)
AML_SIGNATURE = "<CAEXFile"


def _aml_order_numbers(text: str) -> list[str]:
    """从 AML 的 TypeIdentifier 属性里提取 `OrderNumber:...` 形式的订货号。"""
    numbers: set[str] = set()
    for match in AML_TYPE_IDENTIFIER_PATTERN.finditer(text):
        value = match.group(1).strip()
        if value.lower().startswith("ordernumber:"):
            numbers.add(value.split(":", 1)[1].strip())
    return sorted(numbers)


def _normalize_ipv4(value: Any, field: str) -> str:
    """校验并规范化单个 IPv4 地址。"""
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} 不能为空。")
    try:
        return str(ipaddress.IPv4Address(text))
    except ValueError as exc:
        raise ValueError(f"{field} 不是有效的 IPv4 地址：{value!r}") from exc


def _normalize_ipv4_mask(value: Any) -> str:
    """接受 255.255.255.0 或 24 两种写法，统一返回点分掩码。"""
    text = str(value or "").strip()
    if not text:
        raise ValueError("subnet_mask 不能为空。")
    if text.isdigit():
        prefix = int(text)
        if not 0 <= prefix <= 32:
            raise ValueError(f"子网掩码前缀长度必须在 0-32：{value!r}")
        return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)
    try:
        return str(ipaddress.IPv4Network(f"0.0.0.0/{text}").netmask)
    except ValueError as exc:
        raise ValueError(f"子网掩码无效：{value!r}") from exc


def _aml_network_addresses(text: str) -> list[str]:
    """列出 AML 中所有 NetworkAddress 取值。"""
    return [
        match.group(2).strip()
        for match in AML_NETWORK_ADDRESS_PATTERN.finditer(text)
    ]


def _patch_aml_network(
    text: str,
    ip_address: str | None = None,
    subnet_mask: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """改写 AML 中的网络地址/子网掩码，返回新文本与改动摘要。

    IP 只在 AML 里恰好存在一个 NetworkAddress 时改写；多个以太网节点时语义不唯一
    （不知道要改哪个），必须拒绝，避免改错设备。
    """
    summary: dict[str, Any] = {
        "ip_address": None,
        "subnet_mask": None,
        "network_address_before": None,
        "network_address_after": None,
        "network_address_count": 0,
        "order_numbers": [],
    }
    if AML_SIGNATURE not in text:
        raise ValueError("aml_path 不是 TIA CAx(AML) 文件：未找到 CAEXFile 根元素。")

    addresses = _aml_network_addresses(text)
    summary["network_address_count"] = len(addresses)
    summary["network_address_before"] = addresses[0] if addresses else None
    summary["order_numbers"] = _aml_order_numbers(text)

    if ip_address is not None:
        normalized_ip = _normalize_ipv4(ip_address, "ip_address")
        if not addresses:
            raise ValueError(
                "AML 中没有 NetworkAddress 属性，无法写入 IP；"
                "请确认模板里包含 PROFINET 接口节点（如 E1）。"
            )
        if len(addresses) > 1:
            raise ValueError(
                f"AML 中存在 {len(addresses)} 个 NetworkAddress（多个以太网节点）："
                f"{addresses}。为避免改错节点，本工具拒绝批量改写 IP。"
            )
        text = AML_NETWORK_ADDRESS_PATTERN.sub(
            lambda match: f"{match.group(1)}{normalized_ip}{match.group(3)}",
            text,
            count=1,
        )
        summary["ip_address"] = normalized_ip

    if subnet_mask is not None:
        normalized_mask = _normalize_ipv4_mask(subnet_mask)
        if not AML_SUBNET_MASK_PATTERN.search(text):
            raise ValueError("AML 中没有 SubnetMask 属性，无法写入子网掩码。")
        text = AML_SUBNET_MASK_PATTERN.sub(
            lambda match: f"{match.group(1)}{normalized_mask}{match.group(3)}",
            text,
        )
        summary["subnet_mask"] = normalized_mask

    remaining = _aml_network_addresses(text)
    summary["network_address_after"] = remaining[0] if remaining else None
    return text, summary


def declared_scl_blocks(source_text: str) -> list[str]:
    """提取 SCL 源码中按顺序声明且不重复的程序块名称。"""
    result: list[str] = []
    seen: set[str] = set()
    for match in SCL_BLOCK_PATTERN.finditer(source_text):
        name = (match.group(1) or match.group(2)).strip()
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            result.append(name)
    return result


def _call_messages(result: Any, method_name: str) -> list[str]:
    """安全读取原生执行结果中的消息列表。"""
    method = getattr(result, method_name, None)
    if not callable(method):
        return []
    try:
        values = method()
    except Exception:
        return []
    return [str(value) for value in values] if values is not None else []


def execution_result_to_dict(result: Any) -> dict[str, Any]:
    """将原生执行结果转换为可序列化的字典。"""
    try:
        state = str(result.get_result_state())
    except Exception:
        state = "unknown"
    information = _call_messages(result, "get_information")
    warnings = _call_messages(result, "get_warnings")
    errors = _call_messages(result, "get_errors")
    all_messages = _call_messages(result, "get_all_messages")
    return {
        "ok": not errors,
        "state": state,
        "information": information,
        "warnings": warnings,
        "errors": errors,
        "all_messages": all_messages,
    }


def _safe_get_name(obj: Any) -> str:
    """尽可能读取 TIA 对象名称。"""
    getter = getattr(obj, "get_name", None)
    if callable(getter):
        try:
            return str(getter())
        except Exception:
            pass
    property_getter = getattr(obj, "get_property", None)
    if callable(property_getter):
        for candidate in ("Name", "DeviceName", "ProjectName"):
            try:
                value = property_getter(name=candidate)
            except Exception:
                continue
            if value:
                return str(value)
    return type(obj).__name__


def _safe_properties(obj: Any) -> dict[str, str]:
    """尽可能读取 TIA 对象的属性。"""
    names_getter = getattr(obj, "get_properties", None)
    value_getter = getattr(obj, "get_property", None)
    if not callable(names_getter) or not callable(value_getter):
        return {}
    try:
        names = list(names_getter())
    except Exception:
        return {}
    values: dict[str, str] = {}
    for name in names:
        try:
            value = value_getter(name=str(name))
        except Exception:
            continue
        if value is not None:
            values[str(name)] = str(value)
    return values


def _object_summary(obj: Any, include_properties: bool = False) -> dict[str, Any]:
    """生成 TIA 对象的基本信息摘要。"""
    result: dict[str, Any] = {"name": _safe_get_name(obj)}
    for method_name, key in (
        ("get_identifier", "identifier"),
        ("get_path", "path"),
        ("get_path_full", "full_path"),
    ):
        method = getattr(obj, method_name, None)
        if callable(method):
            try:
                result[key] = str(method())
            except Exception:
                pass
    if include_properties:
        result["properties"] = _safe_properties(obj)
    return result


class TiaService:
    """封装当前进程中的 TIA Portal 连接和工程操作。"""

    # attach 遇到 TIA 忙（TargetInvocationException）时的重试次数与间隔。
    ATTACH_ATTEMPTS = 3
    ATTACH_RETRY_SECONDS = 1.5

    def __init__(self) -> None:
        """初始化连接状态；不依赖进程工作目录。"""
        self._lock = threading.RLock()
        self._ts: Any | None = None
        self.portal: Any | None = None
        self.project: Any | None = None
        self.selected_plc_name: str | None = None
        self.connected_version: str | None = None

    def _library(self) -> Any:
        """按需加载西门子 TIA 脚本库。"""
        if self._ts is None:
            missing = openness_assembly_missing()
            if missing:
                raise RuntimeError(
                    "siemens_tia_scripting 缺少 Openness 程序集："
                    + "、".join(missing)
                    + "。打包版请确认 mcp\\_internal 下带有这些 Siemens.TiaPortal.*.dll"
                    "（缺少时调用 .NET 工具会让整个服务以 0xE0434352 退出）；"
                    "开发环境请重新安装 siemens_tia_scripting。"
                )
            try:
                import siemens_tia_scripting as ts
            except ImportError as exc:
                raise RuntimeError(
                    "siemens_tia_scripting 未安装在 MCP Server 使用的 Python 环境中。"
                ) from exc
            self._ts = ts
        return self._ts

    @staticmethod
    def openness_prerequisites() -> dict[str, Any]:
        """检查当前 Windows 登录会话是否具备 Openness 用户组成员资格。"""
        current_user = getpass.getuser()
        base: dict[str, Any] = {
            "required_group": OPENNESS_GROUP_NAME,
            "current_user": current_user,
            "platform_supported": os.name == "nt",
            "action_steps": [
                "打开“计算机管理 → 本地用户和组 → 组”。",
                f"把当前用户加入“{OPENNESS_GROUP_NAME}”组。",
                "注销并重新登录 Windows（仅重启 MCP 通常不会刷新登录令牌）。",
                "重新启动 TIA Portal 和 MCP 后再次连接。",
            ],
        }
        if os.name != "nt":
            return {
                **base,
                "ok": False,
                "group_membership": "unknown",
                "needs_user_action": True,
                "message": "TIA Portal Openness 仅支持 Windows，当前系统无法检查用户组。",
            }

        try:
            completed = subprocess.run(
                ["whoami.exe", "/groups", "/fo", "csv", "/nh"],
                check=False,
                capture_output=True,
                timeout=15,
            )
        except Exception as exc:
            return {
                **base,
                "ok": False,
                "group_membership": "unknown",
                "needs_user_action": False,
                "message": "无法读取当前 Windows 登录令牌中的用户组；连接时仍会继续尝试。",
                "diagnostic": str(exc),
            }

        def decode_output(value: Any) -> str:
            if isinstance(value, str):
                return value
            if not isinstance(value, (bytes, bytearray)):
                return str(value or "")
            for encoding in (
                "utf-8-sig",
                locale.getpreferredencoding(False),
                "mbcs",
            ):
                try:
                    return bytes(value).decode(encoding)
                except (LookupError, UnicodeDecodeError):
                    continue
            return bytes(value).decode("utf-8", errors="replace")

        stdout = decode_output(completed.stdout)
        stderr = decode_output(completed.stderr)
        if completed.returncode != 0:
            return {
                **base,
                "ok": False,
                "group_membership": "unknown",
                "needs_user_action": False,
                "message": "whoami 无法读取当前 Windows 登录令牌中的用户组；连接时仍会继续尝试。",
                "diagnostic": (stderr or stdout).strip(),
            }

        group_names: list[str] = []
        for row in csv.reader(io.StringIO(stdout)):
            if not row:
                continue
            group_name = row[0].strip()
            if group_name:
                group_names.append(group_name)
        expected = OPENNESS_GROUP_NAME.casefold()
        is_member = any(
            name.casefold() == expected
            or name.replace("/", "\\").rsplit("\\", 1)[-1].casefold() == expected
            for name in group_names
        )
        if is_member:
            return {
                **base,
                "ok": True,
                "group_membership": "member",
                "needs_user_action": False,
                "message": f"当前用户已属于“{OPENNESS_GROUP_NAME}”组。",
            }
        return {
            **base,
            "ok": False,
            "group_membership": "missing",
            "needs_user_action": True,
            "message": (
                f"当前用户 {current_user!r} 不在“{OPENNESS_GROUP_NAME}”组，"
                "TIA Portal Openness 连接将被拒绝。请完成 action_steps 后重试。"
            ),
        }

    def _require_openness_prerequisites(self) -> dict[str, Any]:
        """缺少 Openness 用户组时在加载 Siemens DLL 前给出明确错误。"""
        result = self.openness_prerequisites()
        if result.get("group_membership") == "missing":
            steps = "；".join(result["action_steps"])
            raise RuntimeError(f"{result['message']} 操作步骤：{steps}")
        if not result.get("platform_supported"):
            raise RuntimeError(str(result["message"]))
        return result

    @staticmethod
    def _available_adapters(ts: Any) -> list[TiaVersion]:
        """返回当前 Python 包随附的 Openness 适配器版本。"""
        module_file = getattr(ts, "__file__", None)
        if not module_file:
            return []
        return available_adapter_versions(Path(str(module_file)).resolve().parent)

    @classmethod
    def _running_candidates(
        cls, ts: Any, requested: str | None
    ) -> list[TiaVersion]:
        """返回需要探测的版本；auto 只探测本机安装且有适配器的版本。"""
        explicit = normalize_requested_version(requested)
        if explicit is not None:
            return [cls._resolve_connection_version(ts, explicit.api_version, None)]
        installed = set(cls._installed_tia_versions(ts))
        adapters = set(cls._available_adapters(ts))
        if installed and adapters:
            candidates = installed & adapters
        else:
            candidates = installed or adapters
        if not candidates:
            raise RuntimeError(
                f"未检测到可用的 {SUPPORTED_DESCRIPTION}。请确认已安装 TIA Portal V21，"
                "且 siemens_tia_scripting 包含 V21 Openness 适配器。本项目仅支持 V21，"
                "V15-V20 等其它版本会被拒绝连接。"
            )
        return sorted(candidates, reverse=True)

    @classmethod
    def _attach_running_portals(
        cls, ts: Any, mode: Any, requested: str | None
    ) -> tuple[list[tuple[TiaVersion, Any]], list[dict[str, str]]]:
        """尝试附加已启动的 Portal；不负责保留或释放成功的连接。

        TIA 正在编译/保存工程，或上一条 Openness 会话刚被强制结束时，
        ``attach_portal`` 会抛 ``TargetInvocationException``，通常等一两秒就会恢复，
        所以这里做有限次重试，避免把瞬时忙当成「打包版连不上」。
        """
        found: list[tuple[TiaVersion, Any]] = []
        errors: list[dict[str, str]] = []
        for target in cls._running_candidates(ts, requested):
            last_error = ""
            for attempt in range(1, cls.ATTACH_ATTEMPTS + 1):
                try:
                    portal = ts.attach_portal(
                        portal_mode=mode, version=target.api_version
                    )
                except Exception as exc:
                    last_error = str(exc)
                    if attempt < cls.ATTACH_ATTEMPTS:
                        time.sleep(cls.ATTACH_RETRY_SECONDS)
                        continue
                    break
                if portal is not None:
                    found.append((target, portal))
                    last_error = ""
                else:
                    last_error = "attach_portal 未返回实例（TIA 可能正在切换工程）"
                break
            if last_error:
                errors.append({"version": target.api_version, "error": last_error})
        return found, errors

    @staticmethod
    def _is_tia_product_name(name: str) -> bool:
        """判断产品名是否属于 TIA Portal / STEP 7 本体。

        S7-PLCSIM、Startdrive 等附带产品的版本号也可能是 21.0，不能仅凭版本号
        就把它们标记成「可连接 V21」。
        """
        normalized = str(name or "").casefold()
        return any(
            marker in normalized
            for marker in ("step 7", "tia portal", "totally integrated automation")
        )

    @staticmethod
    def _detected_tia_versions(ts: Any) -> list[TiaVersion]:
        """从产品清单中提取所有能识别的 TIA 版本（含未支持的 V15-V20）。"""
        values: list[str] = []
        try:
            for product in ts.get_installed_products():
                if not TiaService._is_tia_product_name(str(product.get_name())):
                    continue
                values.extend(
                    [
                        str(product.get_version()),
                        str(product.get_release()),
                    ]
                )
        except Exception:
            return []
        found: set[TiaVersion] = set()
        for value in values:
            version = peek_tia_version(value)
            if version is not None:
                found.add(version)
        return sorted(found)

    @classmethod
    def _installed_tia_versions(cls, ts: Any) -> list[TiaVersion]:
        """只返回本机已安装的受支持版本（TIA Portal V21）。"""
        return [
            version
            for version in cls._detected_tia_versions(ts)
            if is_supported_version(version)
        ]

    @classmethod
    def _resolve_connection_version(
        cls,
        ts: Any,
        requested: str | None,
        project_path: Path | None,
    ) -> TiaVersion:
        """按工程扩展名、显式参数、已安装最新版的顺序确定适配器。"""
        explicit = normalize_requested_version(requested)
        project_version = infer_project_version(project_path) if project_path else None
        if explicit and project_version and explicit != project_version:
            raise ValueError(
                f"工程扩展名表示 {project_version.label}，但 version 指定为 "
                f"{explicit.label}；请改用 version='auto' 或传入一致版本。"
            )
        target = project_version or explicit
        if target is None:
            target = newest_version(
                version.api_version for version in cls._installed_tia_versions(ts)
            )
        if target is None:
            raise RuntimeError(
                f"未检测到已安装的 {SUPPORTED_DESCRIPTION}。本项目仅支持 V21，"
                "V15-V20 及其它版本已被禁止连接；请先安装并启动 TIA Portal V21。"
            )
        target = ensure_supported(target)

        adapters = cls._available_adapters(ts)
        if adapters and target not in adapters:
            available = ", ".join(item.label for item in adapters)
            raise RuntimeError(
                f"当前 siemens_tia_scripting 未包含 {target.label} 适配器；"
                f"包内可用适配器：{available}。请安装包含该版本适配器的库后重试。"
            )
        return target

    def connected_tia_version(self) -> TiaVersion:
        if not self.connected_version:
            raise RuntimeError("尚未连接 TIA Portal，无法判断版本能力。")
        return parse_tia_version(self.connected_version)

    @staticmethod
    def openness_access_guide(version: str | TiaVersion | None = None) -> dict[str, Any]:
        """说明 Siemens Openness 防火墙授权提示及正确处理方式。"""
        target = parse_tia_version(version) if version else None
        return {
            "ok": True,
            "version": target.api_version if target else None,
            "title": "TIA Portal Openness 访问授权",
            "is_error": False,
            "message": (
                "首次连接、MCP EXE 被更新或移动后，TIA Portal 弹出 Openness 访问提示"
                "属于 Siemens 的正常安全机制，不是版本适配器错误。"
            ),
            "action": (
                "确认正在运行的程序确为 tia-portal-v21-mcp.exe 后，选择“是，全部接受”"
                "（Yes to all），并在随后出现的 Windows 用户帐户控制窗口中选择“是”。"
            ),
            "reappears_when": [
                "只选择了“是”（Yes），没有保存长期授权",
                "EXE 路径发生变化",
                "EXE 修改时间发生变化",
                "升级或重新打包导致 EXE 文件哈希发生变化",
            ],
            "security": "MCP 不会自动点击、绕过或写入 Openness 防火墙授权。",
        }

    def running_tia_portals(
        self,
        version: str = "auto",
        portal_mode: str = "any-ui",
        probe_openness: bool = True,
    ) -> dict[str, Any]:
        """返回 OS 进程；可选用 Openness 探测并立即释放临时连接。"""
        os_processes = running_tia_portals()
        portal_processes = [
            item for item in os_processes if item.get("role") == "portal"
        ]
        recommended_pid = next(
            (item["pid"] for item in os_processes if item.get("recommended")), None
        )
        if not probe_openness:
            return {
                "ok": True,
                "running": bool(portal_processes),
                "os_processes": os_processes,
                "recommended_process_id": recommended_pid,
                "portals": [],
                "probe_errors": [],
                "openness_probed": False,
                "message": (
                    "已从 Windows 进程表发现 TIA Portal；可把 recommended_process_id "
                    "传给 connect_tia(portal_process_id=...)。"
                    if portal_processes
                    else "Windows 进程表中没有 TIA Portal；请先启动 V21 并打开工程。"
                ),
            }
        with self._lock:
            prerequisites = self.openness_prerequisites()
            found: list[tuple[TiaVersion, Any]] = []
            errors: list[dict[str, str]] = []
            if prerequisites.get("group_membership") == "missing":
                errors.append({"stage": "prerequisites", "error": prerequisites["message"]})
            else:
                try:
                    ts = self._library()
                    mode = self._portal_mode(ts, portal_mode)
                    found, errors = self._attach_running_portals(ts, mode, version)
                except Exception as exc:
                    errors.append({"stage": "openness_probe", "error": str(exc)})
            portals: list[dict[str, Any]] = []
            for target, portal in found:
                item: dict[str, Any] = {"version": target.api_version, "label": target.label}
                try:
                    item["process_id"] = int(portal.get_process_id())
                except Exception as exc:
                    item["process_id_error"] = str(exc)
                try:
                    project = portal.get_project()
                    item["has_open_project"] = project is not None
                    if project is not None:
                        item["project"] = _object_summary(project)
                except Exception as exc:
                    item["project_error"] = str(exc)
                portals.append(item)
                try:
                    portal.detach()
                except Exception as exc:
                    item["detach_warning"] = str(exc)
            return {
                "ok": True,
                "running": bool(portal_processes),
                "os_processes": os_processes,
                "recommended_process_id": recommended_pid,
                "portals": portals,
                "probe_errors": errors,
                "openness_probed": True,
                "openness_prerequisites": prerequisites,
                "message": (
                    "已发现并通过 Openness 探测到正在运行的 TIA Portal。"
                    if portals
                    else (
                        "Windows 已发现 TIA Portal 进程，但 Openness 尚未成功附加；"
                        "请使用 recommended_process_id 调用 connect_tia，并查看 probe_errors。"
                        if portal_processes
                        else "未发现 TIA Portal；请先启动目标版本并等待其完全加载。"
                    )
                ),
            }

    @staticmethod
    def project_creation_capabilities() -> dict[str, Any]:
        """公开项目创建与硬件组态的真实能力边界。"""
        return {
            "ok": True,
            "supported_tia": SUPPORTED_DESCRIPTION,
            "create_project_in_running_version": True,
            "detect_running_versions": True,
            "configure_existing_devices": True,
            "configure_existing_device_method": (
                "先读取 get_hardware_inventory，再对用户选定的 device/plc/module 调用 "
                "set_hardware_properties；仅能写入 Openness 实际暴露且可写的属性。"
            ),
            "create_device_from_hardware_catalog": False,
            "create_device_from_cax_aml": True,
            "create_device_via_aml_method": (
                "add_device_from_aml / add_device_from_template：桥接层没有 "
                "CreateWithItem，但 export_cax_data + import_cax_data 能按 AML 真正建出"
                "设备与模块，并可在导入前改写 PROFINET IP 与子网掩码。"
            ),
            "set_device_ip_method": (
                "IP 只能随 AML 导入一并写入（add_device_from_aml(ip_address=...)）；"
                "对已存在设备直接 SetAttribute('IPAddress') 会被 Openness 拒绝"
                "（'IPAddress' is not supported by type 'DeviceItemImpl'，rc=20）。"
            ),
            "plug_or_replace_catalog_module": False,
            "unsupported_reason": (
                "当前 siemens_tia_scripting 桥接层提供 Portal.create_project，"
                "但没有暴露 Project.create_device、硬件目录检索或模块插槽 plug 接口。"
            ),
            "manual_fallback": (
                "新建空工程后，请在 TIA Portal 中人工添加目录设备/模块；随后 MCP 可继续"
                "读取并组态已存在设备的可写属性。若属性位于桥接层未暴露的深层组态页，"
                "工具会明确返回不可达并要求人工组态。"
            ),
            "project_library_note": (
                "桥接层有通用 MasterCopy.instantiate，但它依赖工程/全局库中预先存在且匹配"
                "版本的主副本，不能替代硬件目录选型，因此本 MCP 不把它声明为通用创建设备能力。"
            ),
        }

    def default_programming_policy(self) -> dict[str, Any]:
        """返回桌面控制台配置的 PLC 编程偏好。"""
        version = self.connected_tia_version() if self.connected_version else None
        compact_lad = True if version else None
        result = {
            "ok": True,
            "connected_version": version.api_version if version else None,
            "compact_lad_available": compact_lad,
        }
        result.update(programming_policy())
        return result

    def compatibility(self) -> dict[str, Any]:
        """返回当前连接的版本能力；未连接时返回 V21 能力矩阵。"""
        if self.connected_version:
            version = self.connected_tia_version()
            result = version.capabilities()
            result["openness_access"] = self.openness_access_guide(version)
            result["default_programming_policy"] = self.default_programming_policy()
            result["version_policy"] = version_policy()
            return result
        return {
            "connected": False,
            "supported_versions": compatibility_matrix(),
            "version_policy": version_policy(),
            "openness_access": self.openness_access_guide(),
            "default_programming_policy": self.default_programming_policy(),
        }

    def require_simatic_sd_lad(self) -> TiaVersion:
        """V21 全系列原生支持紧凑 LAD -> SIMATIC SD，返回当前连接版本。"""
        return self.connected_tia_version()

    @staticmethod
    def _portal_mode(ts: Any, mode: str) -> Any:
        """将入口参数转换为 TIA 的界面模式枚举。"""
        normalized = mode.strip().casefold().replace("_", "-")
        mapping = {
            "any": ts.Enums.PortalMode.AnyUserInterface,
            "any-ui": ts.Enums.PortalMode.AnyUserInterface,
            "gui": ts.Enums.PortalMode.WithGraphicalUserInterface,
            "with-gui": ts.Enums.PortalMode.WithGraphicalUserInterface,
            "headless": ts.Enums.PortalMode.WithoutGraphicalUserInterface,
            "without-gui": ts.Enums.PortalMode.WithoutGraphicalUserInterface,
        }
        try:
            return mapping[normalized]
        except KeyError as exc:
            raise ValueError("portal_mode 必须是 any-ui、gui 或 headless。") from exc

    def connect(
        self,
        version: str = "auto",
        portal_mode: str = "any-ui",
        project_file_path: str | None = None,
        plc_name: str | None = None,
        portal_process_id: int | None = None,
    ) -> dict[str, Any]:
        """连接 TIA Portal，并可用 OS 进程号校验目标实例。"""
        with self._lock:
            prerequisites = self._require_openness_prerequisites()
            requested_pid: int | None = None
            if portal_process_id is not None:
                try:
                    requested_pid = int(portal_process_id)
                except (TypeError, ValueError) as exc:
                    raise ValueError("portal_process_id 必须是正整数。") from exc
                if requested_pid <= 0:
                    raise ValueError("portal_process_id 必须是正整数。")
                os_portals = running_tia_portals()
                available = [
                    int(item["pid"])
                    for item in os_portals
                    if item.get("role") == "portal"
                ]
                if requested_pid not in available:
                    raise RuntimeError(
                        f"指定的 TIA Portal 进程 {requested_pid} 当前不存在；"
                        f"Windows 检测到的 Portal PID：{available}。"
                    )
            ts = self._library()
            mode = self._portal_mode(ts, portal_mode)
            path: Path | None = None
            if project_file_path:
                path = absolute_path(project_file_path, "project_file_path")
                if not path.is_file():
                    raise FileNotFoundError(f"找不到 TIA 工程文件：{path}")
            if path is not None:
                target_version = self._resolve_connection_version(ts, version, path)
                print(
                    f"[{target_version.label}] 正在请求 TIA Portal Openness 访问。"
                    "若 TIA 弹出授权窗口，这是正常安全确认；确认程序路径无误后请选择"
                    "“是，全部接受”(Yes to all)，并确认随后出现的 Windows 用户帐户控制窗口。",
                    flush=True,
                )
                project = ts.open_attach_project(
                    project_file_path=str(path), portal_mode=mode
                )
                portal = project.get_portal()
                if requested_pid is not None:
                    actual_pid = int(portal.get_process_id())
                    if actual_pid != requested_pid:
                        try:
                            portal.detach()
                        except Exception:
                            pass
                        raise RuntimeError(
                            f"按工程路径连接到了 TIA PID {actual_pid}，"
                            f"与指定的 portal_process_id={requested_pid} 不一致；"
                            "为避免控制错误工程，已主动断开。"
                        )
            else:
                found, probe_errors = self._attach_running_portals(ts, mode, version)
                if not found:
                    requested = normalize_requested_version(version)
                    label = requested.label if requested else "V21"
                    busy_hint = ""
                    if any(
                        "TargetInvocationException" in str(item.get("error", ""))
                        for item in probe_errors
                    ):
                        busy_hint = (
                            "（TIA Portal 可能正在编译、保存或弹着对话框，"
                            "也可能上一条 Openness 会话刚被强制结束；已自动重试 "
                            f"{self.ATTACH_ATTEMPTS} 次，请等 10–30 秒后再调用 connect_tia）"
                        )
                    raise RuntimeError(
                        f"未发现可附加的 TIA Portal {label} 实例。本项目仅支持 V21，"
                        f"请先启动 TIA Portal V21 并等待其完全加载{busy_hint}；"
                        f"版本探测诊断：{probe_errors}"
                    )
                if requested_pid is not None and found:
                    matching: list[tuple[TiaVersion, Any]] = []
                    attached_pids: list[int] = []
                    for item in found:
                        try:
                            attached_pid = int(item[1].get_process_id())
                        except Exception:
                            attached_pid = -1
                        attached_pids.append(attached_pid)
                        if attached_pid == requested_pid:
                            matching.append(item)
                        else:
                            try:
                                item[1].detach()
                            except Exception:
                                pass
                    if not matching:
                        raise RuntimeError(
                            f"底层 TIA Scripting 仅按版本附加，实际返回 PID {attached_pids}，"
                            f"与指定 PID {requested_pid} 不一致。已断开以避免控制错误工程；"
                            "请关闭无关 TIA 实例，或传入目标工程 project_file_path 后重试。"
                        )
                    found = matching
                if (
                    requested_pid is None
                    and len(found) > 1
                    and normalize_requested_version(version) is None
                ):
                    labels = [item.label for item, _ in found]
                    for _, attached in found:
                        try:
                            attached.detach()
                        except Exception:
                            pass
                    raise RuntimeError(
                        f"检测到多个正在运行的 TIA Portal V21 子版本：{labels}。"
                        "请显式传入 version（21.0 或 21.1），MCP 不会替用户猜测目标版本。"
                    )
                target_version, portal = found[0]
                print(
                    f"[{target_version.label}] 已按当前正在运行的 TIA Portal 版本附加。"
                    "若 TIA 弹出授权窗口，这是正常安全确认；请核对程序路径后选择"
                    "“是，全部接受”(Yes to all)。",
                    flush=True,
                )
                project = portal.get_project()
                if project is None:
                    # 连接失败不能把 Portal 留在附加状态，否则后续 connect 可能
                    # 会话泄漏或出现「已附加但没有项目」的假象。
                    try:
                        portal.detach()
                    except Exception:
                        pass
                    raise RuntimeError(
                        "已连接 TIA Portal，但当前没有打开工程。"
                        "请先在 TIA 中打开工程，或调用 create_tia_project 新建工程。"
                    )

            self.portal = portal
            self.project = project
            self.connected_version = target_version.api_version
            self.selected_plc_name = None
            if plc_name:
                self.select_plc(plc_name)
            else:
                plcs = list(project.get_plcs())
                if len(plcs) == 1:
                    self.selected_plc_name = str(plcs[0].get_name())
            result = self.status()
            result["openness_prerequisites"] = prerequisites
            return result

    def create_project(
        self,
        target_directory: str,
        project_name: str,
        version: str = "auto",
        portal_mode: str = "gui",
        overwrite_existing: bool = False,
        confirm_create: bool = False,
    ) -> dict[str, Any]:
        """按当前已启动的 TIA 版本创建工程，并把新工程设为当前连接。"""
        if not confirm_create:
            raise ValueError("创建 TIA 工程必须设置 confirm_create=true。")
        if not isinstance(project_name, str) or not project_name.strip():
            raise ValueError("project_name 必须是非空字符串。")
        project_name = project_name.strip()
        if project_name in {".", ".."} or any(
            character in project_name for character in '<>:"/\\|?*'
        ):
            raise ValueError("project_name 不能包含 Windows 文件名禁用字符。")
        if project_name.endswith((".", " ")):
            raise ValueError("project_name 不能以点或空格结尾。")

        with self._lock:
            prerequisites = self._require_openness_prerequisites()
            target = output_path(target_directory, "target_directory")
            target.mkdir(parents=True, exist_ok=True)
            ts = self._library()
            mode = self._portal_mode(ts, portal_mode)
            found, probe_errors = self._attach_running_portals(ts, mode, version)
            if not found:
                requested = normalize_requested_version(version)
                label = requested.label if requested else "V21"
                raise RuntimeError(
                    f"未发现已启动的 TIA Portal {label}；本项目仅支持 V21，"
                    f"不会自动猜测或启动其它版本。请先启动 TIA Portal V21。探测诊断：{probe_errors}"
                )
            if len(found) > 1 and normalize_requested_version(version) is None:
                labels = [item.label for item, _ in found]
                for _, attached in found:
                    try:
                        attached.detach()
                    except Exception:
                        pass
                raise RuntimeError(
                    f"同时检测到多个 TIA Portal V21 子版本：{labels}。"
                    "请显式传入 version（21.0 或 21.1）后重试。"
                )

            target_version, portal = found[0]
            opened_new_portal = False
            try:
                current_project = portal.get_project()
            except Exception:
                current_project = None
            if current_project is not None:
                # 一个 Portal 实例同时只能承载一个活动工程。为避免关闭用户已有工程，
                # 在同版本中新开一个实例承载新工程。
                try:
                    portal.detach()
                except Exception:
                    pass
                portal = ts.open_portal(
                    portal_mode=mode, version=target_version.api_version
                )
                opened_new_portal = True

            print(
                f"[{target_version.label}] 正在创建工程 {project_name!r}。"
                "若出现 Openness 授权窗口，请核对程序路径后人工确认。",
                flush=True,
            )
            project = portal.create_project(
                target_directory_path=str(target),
                project_name=project_name,
                delete_existing_project=overwrite_existing,
            )
            self.portal = portal
            self.project = project
            self.connected_version = target_version.api_version
            self.selected_plc_name = None
            return {
                "ok": True,
                "created": True,
                "version": target_version.api_version,
                "project_name": project_name,
                "target_directory": str(target),
                "opened_new_portal": opened_new_portal,
                "overwrite_existing": overwrite_existing,
                "project": _object_summary(project, include_properties=True),
                "device_count": len(list(project.get_devices())),
                "plc_count": len(list(project.get_plcs())),
                "openness_prerequisites": prerequisites,
                "hardware_capabilities": self.project_creation_capabilities(),
                "next_step": (
                    "空工程没有设备。当前桥接层不能从硬件目录自动新增设备；请在 TIA 中"
                    "人工添加设备，或打开已有设备工程，然后用 get_hardware_inventory 和 "
                    "set_hardware_properties 组态可达属性。"
                ),
            }

    @staticmethod
    def _default_template_directory() -> Path:
        """设备 AML 模板的默认目录：服务安装目录下的 device_templates。"""
        return SERVICE_DIRECTORY / "device_templates"

    def export_project_aml(self, target_file: str) -> dict[str, Any]:
        """把当前工程导出为 CAx(AML)，可直接作为后续新建设备的模板。"""
        with self._lock:
            project = self._require_project()
            target = output_path(target_file, "target_file")
            target.parent.mkdir(parents=True, exist_ok=True)
            log_path = target.with_suffix(".log")
            ok = bool(
                project.export_cax_data(
                    export_file_path=str(target), log_file_path=str(log_path)
                )
            )
            if not ok:
                raise RuntimeError(f"CAx 导出失败，详见日志：{log_path}")
            text = target.read_text(encoding="utf-8", errors="replace")
            return {
                "ok": True,
                "exported": True,
                "file": str(target),
                "log_file": str(log_path),
                "size_bytes": target.stat().st_size,
                "network_addresses": _aml_network_addresses(text),
                "order_numbers": _aml_order_numbers(text),
                "next_step": (
                    "可直接把该 AML 作为模板：add_device_from_aml(aml_path=..., "
                    "ip_address=...)。若整份 AML 含多个设备，导入时会一并创建设备。"
                ),
            }

    def add_device_from_aml(
        self,
        aml_path: str,
        ip_address: str | None = None,
        subnet_mask: str | None = None,
        confirm_import: bool = False,
    ) -> dict[str, Any]:
        """用 CAx(AML) 在当前工程中创建设备，并可在导入前改写 IP/子网掩码。

        这是桥接层缺失 CreateWithItem 时的唯一可行通道：AML 由 TIA 生成，
        import_cax_data 会按 AML 内的订货号真正建出设备与模块。
        """
        if not confirm_import:
            raise ValueError("导入 AML 会新增设备，必须设置 confirm_import=true。")
        with self._lock:
            project = self._require_project()
            source = absolute_path(aml_path, "aml_path")
            if not source.is_file():
                raise ValueError(f"AML 文件不存在：{source}")
            text = source.read_text(encoding="utf-8", errors="strict")
            patched, summary = _patch_aml_network(text, ip_address, subnet_mask)

            for plc in list(project.get_plcs()):
                self._assert_offline(plc)

            before_devices = {str(item.get_name()) for item in project.get_devices()}
            before_plcs = {str(item.get_name()) for item in project.get_plcs()}

            handle, temp_name = tempfile.mkstemp(prefix="mcp_cax_", suffix=".aml")
            os.close(handle)
            temp_path = Path(temp_name)
            log_path = temp_path.with_suffix(".log")
            try:
                temp_path.write_text(patched, encoding="utf-8")
                ok = bool(
                    project.import_cax_data(
                        import_file_path=str(temp_path), log_file_path=str(log_path)
                    )
                )
                log_text = (
                    log_path.read_text(encoding="utf-8", errors="replace")
                    if log_path.is_file()
                    else ""
                )
                if not ok:
                    raise RuntimeError(
                        "CAx 导入失败：" + (log_text.strip()[-800:] or "无日志")
                    )
                devices = list(project.get_devices())
                plcs = list(project.get_plcs())
                new_devices = sorted(
                    {str(item.get_name()) for item in devices} - before_devices
                )
                new_plcs = sorted(
                    {str(item.get_name()) for item in plcs} - before_plcs
                )
                if not new_devices:
                    raise RuntimeError(
                        "CAx 导入返回成功，但没有新增设备；AML 可能只含软件对象。"
                        + (log_text.strip()[-400:] if log_text.strip() else "")
                    )
                return {
                    "ok": True,
                    "imported": True,
                    "source_aml": str(source),
                    "log_file": str(log_path),
                    "import_summary": summary,
                    "new_devices": new_devices,
                    "new_plcs": new_plcs,
                    "device_count": len(devices),
                    "plc_count": len(plcs),
                    "plc_order_numbers": {
                        str(item.get_name()): str(
                            item.get_property(name="OrderNumber")
                        )
                        for item in plcs
                    },
                    "device_state": "未保存。确认无误后调用 save_project。",
                }
            finally:
                for leftover in (temp_path, log_path):
                    try:
                        if leftover.is_file():
                            leftover.unlink()
                    except OSError:
                        pass

    def list_device_templates(
        self, template_directory: str | None = None
    ) -> dict[str, Any]:
        """列出可用于新建设备的 AML 模板及其订货号。"""
        directory = (
            absolute_path(template_directory, "template_directory")
            if template_directory
            else self._default_template_directory()
        )
        if not directory.is_dir():
            return {
                "ok": True,
                "directory": str(directory),
                "templates": [],
                "count": 0,
                "hint": (
                    "模板目录不存在。先用 export_project_aml 从任一含目标机型的工程导出 "
                    "AML，再放到该目录即可按订货号建站。"
                ),
            }
        templates: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.aml")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            templates.append(
                {
                    "file": str(path),
                    "size_bytes": path.stat().st_size,
                    "order_numbers": _aml_order_numbers(text),
                    "network_addresses": _aml_network_addresses(text),
                }
            )
        return {
            "ok": True,
            "directory": str(directory),
            "templates": templates,
            "count": len(templates),
        }

    def add_device_from_template(
        self,
        order_number: str,
        ip_address: str | None = None,
        subnet_mask: str | None = None,
        template_directory: str | None = None,
        confirm_import: bool = False,
    ) -> dict[str, Any]:
        """按订货号在模板目录中匹配 AML，并据此新建设备。"""
        listing = self.list_device_templates(template_directory)
        wanted = re.sub(r"\s+", "", str(order_number or "")).upper()
        if not wanted:
            raise ValueError("order_number 不能为空，例如 6ES7 212-1AG50-0XB0。")
        matches: list[str] = []
        for item in listing["templates"]:
            for number in item["order_numbers"]:
                if re.sub(r"\s+", "", number).upper().startswith(wanted):
                    matches.append(item["file"])
                    break
        if not matches:
            raise ValueError(
                f"模板目录 {listing['directory']} 中没有订货号 {order_number} 的 AML。"
                "请先用 export_project_aml 从含该机型的工程导出模板。"
            )
        if len(matches) > 1:
            raise ValueError(
                f"订货号 {order_number} 匹配到多个模板：{matches}；"
                "请改用 add_device_from_aml 指定文件。"
            )
        return self.add_device_from_aml(
            aml_path=matches[0],
            ip_address=ip_address,
            subnet_mask=subnet_mask,
            confirm_import=confirm_import,
        )

    @contextmanager
    def _clean_child_environment(self) -> Iterator[list[str]]:
        """在启动 TIA 前临时剥离会让子进程误加载捆绑 DLL 的路径。

        PyInstaller 会把 `_internal`（sys._MEIPASS）塞进 PATH，子进程继承后 TIA 会从
        服务目录加载 VCRUNTIME140/MSVCP140，把 release 目录钉死，导致后续重新打包失败。
        这里在调用期间移除这些路径并把工作目录切到临时目录，结束后原样恢复。
        """
        original_path = os.environ.get("PATH", "")
        original_cwd = os.getcwd()
        roots: set[str] = {str(SERVICE_DIRECTORY).casefold().rstrip("\\")}
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.add(str(meipass).casefold().rstrip("\\"))
        prefixes = tuple(f"{root}\\" for root in roots)

        kept: list[str] = []
        removed: list[str] = []
        for entry in original_path.split(os.pathsep):
            if not entry:
                continue
            lowered = entry.casefold().rstrip("\\")
            if lowered in roots or any(
                (lowered + "\\").startswith(prefix) for prefix in prefixes
            ):
                removed.append(entry)
                continue
            kept.append(entry)
        try:
            if removed:
                os.environ["PATH"] = os.pathsep.join(kept)
            os.chdir(tempfile.gettempdir())
            yield removed
        finally:
            os.environ["PATH"] = original_path
            try:
                os.chdir(original_cwd)
            except OSError:
                pass

    @staticmethod
    def _has_visible_window(pid: int) -> bool:
        """判断指定进程是否已出现可见的顶层窗口（TIA 界面就绪的近似判据）。"""
        try:
            import ctypes
            from ctypes import wintypes

            found: list[int] = []
            prototype = ctypes.WINFUNCTYPE(
                ctypes.c_bool, wintypes.HWND, wintypes.LPARAM
            )

            def callback(hwnd: int, lparam: int) -> bool:
                owner = wintypes.DWORD()
                ctypes.windll.user32.GetWindowThreadProcessId(
                    hwnd, ctypes.byref(owner)
                )
                if owner.value != pid:
                    return True
                if not ctypes.windll.user32.IsWindowVisible(hwnd):
                    return True
                if ctypes.windll.user32.GetWindowTextLengthW(hwnd) > 0:
                    found.append(hwnd)
                    return False
                return True

            ctypes.windll.user32.EnumWindows(prototype(callback), 0)
            return bool(found)
        except Exception:
            return False

    @staticmethod
    def _portal_install_directories() -> list[dict[str, Any]]:
        """扫描 Program Files 下的 Portal V* 安装目录，读主程序版本。"""
        roots = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Siemens"
            / "Automation",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Siemens"
            / "Automation",
        ]
        result: list[dict[str, Any]] = []
        for root in roots:
            if not root.is_dir():
                continue
            for directory in sorted(root.glob("Portal V*")):
                exe = directory / "bin" / "Siemens.Automation.Portal.exe"
                if not exe.is_file():
                    continue
                record: dict[str, Any] = {
                    "directory": str(directory),
                    "folder": directory.name,
                    "executable": str(exe),
                    "file_version": None,
                }
                try:
                    import win32api  # type: ignore

                    info = win32api.GetFileVersionInfo(str(exe), "\\")
                    ms = info.get("FileVersionMS", 0)
                    ls = info.get("FileVersionLS", 0)
                    record["file_version"] = "%d.%d.%d.%d" % (
                        ms >> 16,
                        ms & 0xFFFF,
                        ls >> 16,
                        ls & 0xFFFF,
                    )
                except Exception:
                    pass
                result.append(record)
        return result

    def detect_installations(self) -> dict[str, Any]:
        """汇总本机 TIA 的安装版本、运行实例与可连接性，只放行 V21。"""
        with self._lock:
            ts = self._library()
            detected = self._detected_tia_versions(ts)
            installed = [
                version for version in detected if is_supported_version(version)
            ]
            blocked = [
                version for version in detected if not is_supported_version(version)
            ]
            products: list[dict[str, Any]] = []
            try:
                for product in ts.get_installed_products():
                    version_text = str(product.get_version())
                    detected_version = peek_tia_version(version_text)
                    is_tia = self._is_tia_product_name(str(product.get_name()))
                    products.append(
                        {
                            "name": str(product.get_name()),
                            "version": version_text,
                            "supported": bool(
                                is_tia
                                and detected_version is not None
                                and is_supported_version(detected_version)
                            ),
                        }
                    )
            except Exception:
                pass

            running: list[dict[str, Any]] = []
            running_errors: list[dict[str, str]] = []
            try:
                for target, portal in self._attach_running_portals(
                    ts, self._portal_mode(ts, "any-ui"), None
                )[0]:
                    try:
                        pid = int(portal.get_process_id())
                    except Exception:
                        pid = None
                    running.append(
                        {
                            "version": target.api_version,
                            "label": target.label,
                            "pid": pid,
                        }
                    )
                    try:
                        portal.detach()
                    except Exception:
                        pass
            except Exception as exc:
                running_errors.append({"error": str(exc)})

            installed_records = self._portal_install_directories()
            adapter_versions = {
                item.api_version for item in self._available_adapters(ts)
            }
            install_versions = {item.api_version for item in installed}
            connectable = sorted(
                {v for v in (install_versions | adapter_versions) if v},
                reverse=True,
            )

            recommended: dict[str, Any]
            if running:
                recommended = {
                    "action": "connect",
                    "version": running[0]["version"],
                    "reason": "已有正在运行的 V21 实例，直接 connect_tia 即可。",
                }
            elif installed:
                recommended = {
                    "action": "launch",
                    "version": installed[0].api_version,
                    "reason": (
                        "没有运行中的 V21 实例；可用 launch_tia(version=...) 自动启动。"
                        if len(installed) == 1
                        else "安装了多个 V21 子版本，必须显式指定 version（21.0/21.1）再 launch_tia。"
                    ),
                }
            else:
                recommended = {
                    "action": "install",
                    "version": None,
                    "reason": (
                        "未检测到已安装的 TIA Portal V21。本项目仅支持 V21，"
                        "V15-V20 等其它版本会被拒绝连接。"
                    ),
                }

            return {
                "ok": True,
                "supported": SUPPORTED_DESCRIPTION,
                "installed": [
                    {"version": item.api_version, "label": item.label}
                    for item in installed
                ],
                "blocked_installed": [
                    {
                        "version": item.api_version,
                        "label": item.label,
                        "reason": "本项目仅支持 V21，该版本不会出现在可连接列表。",
                    }
                    for item in blocked
                ],
                "install_directories": installed_records,
                "products": products,
                "adapters": sorted(adapter_versions, reverse=True),
                "connectable_versions": connectable,
                "running": running,
                "running_errors": running_errors,
                "recommended": recommended,
                "policy": version_policy(),
            }

    def launch_tia(
        self,
        version: str = "auto",
        project_file_path: str | None = None,
        wait_seconds: int = 120,
        confirm_launch: bool = False,
        force_new: bool = False,
    ) -> dict[str, Any]:
        """启动 TIA Portal（可选直接打开工程），并把当前连接指向它。

        首次连接或 EXE 变更后，TIA 仍会弹出 Openness 授权窗口，必须人工确认。
        """
        if not confirm_launch:
            raise ValueError("启动 TIA Portal 必须设置 confirm_launch=true。")
        with self._lock:
            ts = self._library()
            detected = self._detected_tia_versions(ts)
            installed = [
                version for version in detected if is_supported_version(version)
            ]
            blocked = [
                version for version in detected if not is_supported_version(version)
            ]
            requested = normalize_requested_version(version)
            if requested is None:
                if len(installed) == 1:
                    target = installed[0]
                elif not installed:
                    if blocked:
                        blocked_labels = ", ".join(item.label for item in blocked)
                        raise RuntimeError(
                            f"仅检测到 {blocked_labels}，本项目仅支持 "
                            f"{SUPPORTED_DESCRIPTION}，无法启动其它版本。"
                            "请安装 TIA Portal V21 后重试。"
                        )
                    raise RuntimeError(
                        f"未检测到已安装的 {SUPPORTED_DESCRIPTION}，无法启动。"
                    )
                else:
                    labels = [item.api_version for item in installed]
                    raise RuntimeError(
                        f"安装了多个 V21 子版本 {labels}，必须显式指定 "
                        "version（21.0/21.1）后启动。"
                    )
            else:
                target = requested
                if installed and target.api_version not in {
                    item.api_version for item in installed
                }:
                    raise RuntimeError(
                        f"指定的 {target.label} 未安装；本机已安装的 V21："
                        + ", ".join(item.api_version for item in installed)
                    )

            existing: list[dict[str, Any]] = []
            if not force_new:
                found, _ = self._attach_running_portals(
                    ts, self._portal_mode(ts, "any-ui"), target.api_version
                )
                for item, running_portal in found:
                    try:
                        running_pid = int(running_portal.get_process_id())
                    except Exception:
                        running_pid = None
                    existing.append(
                        {"version": item.api_version, "pid": running_pid}
                    )

            if existing:
                return {
                    "ok": True,
                    "launched": False,
                    "already_running": True,
                    "instances": existing,
                    "version": existing[0]["version"],
                    "message": (
                        "该版本已有运行实例，未重复启动。需要新开实例请传 force_new=true。"
                    ),
                    "next_step": "直接调用 connect_tia 附加即可。",
                }

            moved_paths: list[str] = []
            portal = None
            with self._clean_child_environment() as moved_paths:
                portal = ts.open_portal(
                    portal_mode=self._portal_mode(ts, "gui"),
                    version=target.api_version,
                )
            if portal is None:
                raise RuntimeError("open_portal 未返回可用的 TIA 实例。")

            pid = None
            try:
                pid = int(portal.get_process_id())
            except Exception:
                pid = None

            waited = 0.0
            step = 0.5
            limit = max(float(wait_seconds), 0.0)
            ready = False
            while waited < limit:
                if pid is not None and self._has_visible_window(pid):
                    ready = True
                    break
                threading.Event().wait(step)
                waited += step

            project_info: dict[str, Any] | None = None
            if project_file_path:
                source = absolute_path(project_file_path, "project_file_path")
                if not source.is_file():
                    raise ValueError(f"工程文件不存在：{source}")
                project = portal.open_project(project_file_path=str(source))
                self.portal = portal
                self.project = project
                self.connected_version = target.api_version
                plcs = list(project.get_plcs())
                if plcs:
                    self.selected_plc_name = str(plcs[0].get_name())
                project_info = {
                    "name": str(project.get_property(name="Name")),
                    "path": str(project.get_property(name="Path")),
                    "plc_count": len(plcs),
                }

            return {
                "ok": True,
                "launched": True,
                "version": target.api_version,
                "pid": pid,
                "window_ready": ready,
                "waited_seconds": round(waited, 1),
                "detached_service_paths": moved_paths,
                "project": project_info,
                "connected": project_info is not None,
                "next_step": (
                    "工程已打开并设为当前连接。"
                    if project_info
                    else "实例已启动但未打开工程；请调用 connect_tia(project_file_path=...) 或 create_tia_project。"
                ),
                "note": (
                    "若出现 Openness 授权窗口，请人工选择“是，全部接受”；"
                    "本工具不会自动点击或绕过该安全授权。"
                ),
            }

    def disconnect(
        self, close_portal: bool = False, confirm_close: bool = False
    ) -> dict[str, Any]:
        """断开连接，必要时在确认后关闭 TIA Portal。"""
        with self._lock:
            if self.portal is None:
                return {"ok": True, "connected": False, "message": "当前没有连接。"}
            if close_portal:
                if not confirm_close:
                    raise ValueError("关闭 TIA Portal 必须设置 confirm_close=true。")
                self.portal.close_portal()
                action = "closed"
            else:
                self.portal.detach()
                action = "detached"
            self.portal = None
            self.project = None
            self.selected_plc_name = None
            self.connected_version = None
            return {"ok": True, "connected": False, "action": action}

    def _require_project(self) -> Any:
        """返回当前工程；未连接时给出错误。"""
        if self.project is None:
            raise RuntimeError("尚未连接 TIA Portal，请先调用 connect_tia。")
        return self.project

    def _plcs(self) -> list[Any]:
        """获取当前工程中的所有 PLC。"""
        return list(self._require_project().get_plcs())

    def _require_plc(self, plc_name: str | None = None) -> Any:
        """按名称或当前选择获取唯一目标 PLC。"""
        requested = plc_name or self.selected_plc_name
        plcs = self._plcs()
        if not plcs:
            raise RuntimeError("当前工程中没有找到 PLC。")
        if requested:
            for plc in plcs:
                if str(plc.get_name()).casefold() == requested.casefold():
                    return plc
            available = [str(plc.get_name()) for plc in plcs]
            raise RuntimeError(
                f"工程中不存在 PLC {requested!r}；可选项：{available}"
            )
        if len(plcs) == 1:
            return plcs[0]
        raise RuntimeError("工程包含多台 PLC，请先调用 select_plc 或传入 plc_name。")

    def select_plc(self, plc_name: str) -> dict[str, Any]:
        """选择后续操作使用的默认 PLC。"""
        with self._lock:
            plc = self._require_plc(plc_name)
            self.selected_plc_name = str(plc.get_name())
            return {
                "ok": True,
                "selected_plc": self.selected_plc_name,
                "online_state": str(plc.get_online_state()),
            }

    def status(self) -> dict[str, Any]:
        """读取连接、工程和 PLC 状态。"""
        with self._lock:
            if self.project is None or self.portal is None:
                return {"ok": True, "connected": False}
            project = self.project
            plcs = list(project.get_plcs())
            result: dict[str, Any] = {
                "ok": True,
                "connected": True,
                "portal_process_id": int(self.portal.get_process_id()),
                "version": self.connected_version,
                "compatibility": self.compatibility(),
                "project": _object_summary(project, include_properties=True),
                "plcs": [],
                "selected_plc": self.selected_plc_name,
            }
            for plc in plcs:
                item = _object_summary(plc)
                try:
                    item["online_state"] = str(plc.get_online_state())
                except Exception as exc:
                    item["online_state_error"] = str(exc)
                result["plcs"].append(item)
            return result

    def installed_products(self) -> dict[str, Any]:
        """列出本机已安装的 TIA 产品与产品包，并标注是否属于 V21。"""
        with self._lock:
            ts = self._library()
            products = []
            for product in ts.get_installed_products():
                version_text = str(product.get_version())
                detected = peek_tia_version(version_text)
                is_tia = self._is_tia_product_name(str(product.get_name()))
                products.append(
                    {
                        "name": str(product.get_name()),
                        "version": version_text,
                        "release": str(product.get_release()),
                        "supported": bool(
                            is_tia
                            and detected is not None
                            and is_supported_version(detected)
                        ),
                    }
                )
            bundles = []
            for bundle in ts.get_installed_bundles():
                bundles.append(
                    {
                        "title": str(bundle.get_title()),
                        "release": str(bundle.get_release()),
                        "products": [
                            {
                                "name": str(product.get_name()),
                                "version": str(product.get_version()),
                                "release": str(product.get_release()),
                            }
                            for product in bundle.get_products()
                        ],
                    }
                )
            adapters = self._available_adapters(ts)
            return {
                "ok": True,
                "supported": SUPPORTED_DESCRIPTION,
                "products": products,
                "bundles": bundles,
                "available_openness_adapters": [
                    item.capabilities() for item in adapters
                ],
                "compatibility_matrix": compatibility_matrix(),
                "version_policy": version_policy(),
                "openness_access": self.openness_access_guide(),
                "openness_prerequisites": self.openness_prerequisites(),
                "project_creation_capabilities": self.project_creation_capabilities(),
            }

    def device_configuration(
        self, include_properties: bool = True
    ) -> dict[str, Any]:
        """读取工程中的设备、PLC 和 HMI 组态。"""
        with self._lock:
            project = self._require_project()
            devices = [
                _object_summary(device, include_properties)
                for device in project.get_devices()
            ]
            plcs = [
                _object_summary(plc, include_properties) for plc in project.get_plcs()
            ]
            hmis = [
                _object_summary(hmi, include_properties) for hmi in project.get_hmis()
            ]
            return {
                "ok": True,
                "project": _object_summary(project, include_properties),
                "devices": devices,
                "plcs": plcs,
                "hmis": hmis,
            }

    @staticmethod
    def _normalize_needles(property_name_contains: list[str] | None) -> list[str] | None:
        """把属性名过滤条件归一化为小写片段；None 表示不过滤。"""
        if property_name_contains is None:
            return None
        if not isinstance(property_name_contains, list) or not property_name_contains:
            raise ValueError("property_name_contains 必须是非空字符串数组或 null。")
        needles: list[str] = []
        for value in property_name_contains:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("property_name_contains 只能包含非空字符串。")
            needles.append(value.strip().casefold())
        return needles

    @staticmethod
    def _compact_hardware_name(value: Any) -> str:
        """归一化成小写字母数字串，便于跨 CPU/固件匹配属性名。"""
        return re.sub(r"[^a-z0-9]", "", str(value).casefold())

    @staticmethod
    def _property_filter(
        properties: dict[str, str], property_name_contains: list[str] | None
    ) -> dict[str, str]:
        needles = TiaService._normalize_needles(property_name_contains)
        if needles is None:
            return properties
        return {
            name: value
            for name, value in properties.items()
            if any(needle in name.casefold() for needle in needles)
        }

    @staticmethod
    def _property_scan(obj: Any) -> tuple[list[str], str | None]:
        """读取对象公开的全部属性名；返回 (属性名, 错误说明)。"""
        names_getter = getattr(obj, "get_properties", None)
        if not callable(names_getter):
            return [], "对象不支持 get_properties"
        try:
            return [str(name) for name in names_getter()], None
        except Exception as exc:
            return [], str(exc) or repr(exc)

    def _hardware_targets(self) -> tuple[list[dict[str, Any]], list[str]]:
        """枚举桥接层当前可达的硬件对象。

        siemens_tia_scripting 只在 Device 上提供 ``get_modules()``，Module 没有任何
        子项遍历接口，因此可达边界就是「设备 / PLC / 设备下的模块」这三层；CPU 之下
        更深的组态页（例如 S7-1200 G2 的“高速计数器”页）不在枚举范围内。返回值第二
        项是遍历过程中的告警，用于如实报告可达性而不是假装参数不存在。
        """
        project = self._require_project()
        targets: list[dict[str, Any]] = []
        warnings: list[str] = []
        for device in project.get_devices():
            device_name = _safe_get_name(device)
            targets.append(
                {
                    "target_type": "device",
                    "object": device,
                    "name": device_name,
                    "identifier": self._identifier(device),
                    "device_name": None,
                }
            )
            try:
                modules = list(device.get_modules())
            except Exception as exc:
                warnings.append(
                    f"设备 {device_name!r} 的模块枚举失败：{str(exc) or repr(exc)}"
                )
                modules = []
            for module in modules:
                targets.append(
                    {
                        "target_type": "module",
                        "object": module,
                        "name": _safe_get_name(module),
                        "identifier": self._identifier(module),
                        "device_name": device_name,
                    }
                )
        for plc in project.get_plcs():
            targets.append(
                {
                    "target_type": "plc",
                    "object": plc,
                    "name": _safe_get_name(plc),
                    "identifier": self._identifier(plc),
                    "device_name": None,
                }
            )
        return targets, warnings

    @staticmethod
    def _public_target_summary(target: dict[str, Any]) -> dict[str, Any]:
        return {
            "target_type": target.get("target_type"),
            "name": target.get("name"),
            "identifier": target.get("identifier"),
            "device_name": target.get("device_name"),
        }

    def _property_location_hint(self, names: list[str]) -> list[dict[str, Any]]:
        """指出这些属性名实际出现在哪些可达对象上，取代“请自己再读一遍”的空报错。"""
        wanted = [str(name).casefold() for name in names if isinstance(name, str)]
        if not wanted:
            return []
        hints: list[dict[str, Any]] = []
        targets, _warnings = self._hardware_targets()
        for target in targets:
            available, _error = self._property_scan(target["object"])
            by_casefold = {name.casefold(): name for name in available}
            found = [by_casefold[name] for name in wanted if name in by_casefold]
            if found:
                hint = self._public_target_summary(target)
                hint["property_names"] = found
                hints.append(hint)
        return hints

    def _token_property_targets(self, tokens: tuple[str, ...]) -> list[dict[str, Any]]:
        """在全部可达对象里找属性名带指定 token 的对象（跨 CPU 不硬编码具体属性名）。"""
        found: list[dict[str, Any]] = []
        targets, _warnings = self._hardware_targets()
        for target in targets:
            available, _error = self._property_scan(target["object"])
            matched = [
                name
                for name in available
                if any(token in self._compact_hardware_name(name) for token in tokens)
            ]
            if matched:
                entry = self._public_target_summary(target)
                entry["property_names"] = matched
                found.append(entry)
        return found

    def _hardware_summary(
        self,
        obj: Any,
        include_properties: bool,
        property_name_contains: list[str] | None,
    ) -> dict[str, Any]:
        result = _object_summary(obj, include_properties=False)
        if include_properties:
            all_property_names, error = self._property_scan(obj)
            if error:
                result["property_names_error"] = error
            result["property_count"] = len(all_property_names)
            needles = self._normalize_needles(property_name_contains)
            if needles is None:
                property_names = all_property_names
            else:
                property_names = [
                    name
                    for name in all_property_names
                    if any(needle in name.casefold() for needle in needles)
                ]
            result["property_names"] = property_names
            result["properties"] = self._property_filter(
                _safe_properties(obj), property_name_contains
            )
            unreadable = [
                name for name in property_names if name not in result["properties"]
            ]
            if unreadable:
                result["unreadable_properties"] = unreadable
        return result

    def hardware_inventory(
        self,
        include_properties: bool = True,
        property_name_contains: list[str] | None = None,
    ) -> dict[str, Any]:
        """枚举设备、PLC、模块及其 Openness 动态属性。

        Siemens 的硬件属性随订货号、固件和 TIA 版本变化，因此写入前必须先
        用本接口读取目标实例真实暴露的属性名，不能依靠跨型号硬编码。
        """
        with self._lock:
            if property_name_contains is not None and not include_properties:
                raise ValueError(
                    "使用 property_name_contains 时必须设置 include_properties=true。"
                )
            project = self._require_project()
            devices: list[dict[str, Any]] = []
            for device in project.get_devices():
                item = self._hardware_summary(
                    device, include_properties, property_name_contains
                )
                try:
                    item["modules"] = [
                        self._hardware_summary(
                            module, include_properties, property_name_contains
                        )
                        for module in device.get_modules()
                    ]
                except Exception as exc:
                    item["modules"] = []
                    item["modules_error"] = str(exc) or repr(exc)
                devices.append(item)
            inventory = {
                "ok": True,
                "project": _object_summary(project, include_properties=False),
                "devices": devices,
                "plcs": [
                    self._hardware_summary(
                        plc, include_properties, property_name_contains
                    )
                    for plc in project.get_plcs()
                ],
            }
            inventory["summary"] = self._hardware_inventory_summary(
                inventory, property_name_contains, include_properties
            )
            return inventory

    def _hardware_inventory_summary(
        self,
        inventory: dict[str, Any],
        property_name_contains: list[str] | None,
        include_properties: bool = True,
    ) -> dict[str, Any]:
        """汇总清单结果；过滤零命中时明确说明这是“没有该属性名”而非“没读到”。"""
        objects: list[dict[str, Any]] = []
        for device in inventory.get("devices", []):
            if not isinstance(device, dict):
                continue
            candidates: list[tuple[str, Any]] = [("device", device)]
            candidates.extend(
                ("module", module)
                for module in device.get("modules", [])
                if isinstance(module, dict)
            )
            for target_type, item in candidates:
                objects.append(
                    {
                        "target_type": target_type,
                        "name": item.get("name"),
                        "matched_property_names": list(item.get("property_names", [])),
                        "property_count": item.get("property_count", 0),
                    }
                )
        for plc in inventory.get("plcs", []):
            if not isinstance(plc, dict):
                continue
            objects.append(
                {
                    "target_type": "plc",
                    "name": plc.get("name"),
                    "matched_property_names": list(plc.get("property_names", [])),
                    "property_count": plc.get("property_count", 0),
                }
            )
        matched = [item for item in objects if item["matched_property_names"]]
        total_properties = sum(item["property_count"] for item in objects)
        summary: dict[str, Any] = {
            "objects": len(objects),
            "objects_with_matches": len(matched),
            "properties_enumerated": bool(include_properties),
            # 未枚举属性时用 null 明确表示“没读”，避免被读成“该对象没有属性”。
            "properties_scanned": total_properties if include_properties else None,
            "matched_property_count": (
                sum(len(item["matched_property_names"]) for item in objects)
                if include_properties
                else None
            ),
            "matched_objects": matched,
            "reachability": self._reachability_note(),
        }
        if not include_properties:
            summary["note"] = (
                "include_properties=false：本次只枚举了设备/PLC/模块对象，没有读取属性名。"
                "要判断某个硬件参数是否可达，请传 include_properties=true，"
                "或直接使用 find_hardware_property。"
            )
            return summary
        if property_name_contains is not None and not matched:
            summary["filter_matched_nothing"] = True
            summary["message"] = (
                f"在 {summary['objects']} 个可达硬件对象、"
                f"{summary['properties_scanned']} 个属性名里没有任何名称包含 "
                f"{property_name_contains}。"
            )
            summary["recommendation"] = (
                "这属于可达性边界（该参数不在桥接层暴露的设备/PLC/模块属性里），"
                "不是过滤条件太窄：可先用不带过滤的 get_hardware_inventory 复核全部属性名，"
                "或用 find_hardware_property 扫描；确实需要该参数时请在 TIA Portal 中人工组态，"
                "或升级支持更深层组态页的 siemens_tia_scripting/TIA 版本。"
            )
        return summary

    @staticmethod
    def _reachability_note() -> dict[str, Any]:
        return {
            "layers": ["device", "plc", "module"],
            "nested_device_items": False,
            "note": (
                "桥接层只在 Device 上提供 get_modules()，Module 无子项遍历；"
                "CPU 下更深层的组态页（如 S7-1200 G2 的“高速计数器”页）不在可达范围内。"
            ),
        }

    def find_hardware_property(
        self,
        property_name_contains: list[str] | None = None,
        include_values: bool = True,
        target_type: str | None = None,
        target_name: str | None = None,
        device_name: str | None = None,
    ) -> dict[str, Any]:
        """在全部可达硬件对象里按属性名片段扫描，返回命中位置与当前值。

        这是写入硬件参数前判断“该参数到底可不可达”的手段：零命中意味着桥接层在
        设备/PLC/模块三层都没有暴露该参数，而不是过滤条件太窄。
        """
        with self._lock:
            needles = self._normalize_needles(property_name_contains)
            normalized_type = None
            if target_type is not None:
                normalized_type = str(target_type).strip().casefold()
                if normalized_type not in {"plc", "device", "module"}:
                    raise ValueError("target_type 必须是 plc、device 或 module。")
            targets, warnings = self._hardware_targets()
            wanted_name = (
                str(target_name).strip().casefold() if target_name is not None else None
            )
            wanted_device = (
                str(device_name).strip().casefold() if device_name is not None else None
            )
            summaries: list[dict[str, Any]] = []
            matches: list[dict[str, Any]] = []
            properties_scanned = 0
            for target in targets:
                if (
                    normalized_type is not None
                    and target["target_type"] != normalized_type
                ):
                    continue
                if wanted_name is not None and (
                    (target.get("name") or "").casefold() != wanted_name
                ):
                    continue
                if wanted_device is not None and (
                    (target.get("device_name") or "").casefold() != wanted_device
                ):
                    continue
                names, error = self._property_scan(target["object"])
                properties_scanned += len(names)
                if needles is None:
                    matched = list(names)
                else:
                    matched = [
                        name
                        for name in names
                        if any(needle in name.casefold() for needle in needles)
                    ]
                summary = self._public_target_summary(target)
                summary["property_count"] = len(names)
                summary["matched_property_names"] = matched
                if error:
                    summary["property_names_error"] = error
                summaries.append(summary)
                for name in matched:
                    entry = self._public_target_summary(target)
                    entry["property"] = name
                    if include_values:
                        try:
                            entry["value"] = str(
                                target["object"].get_property(name=name)
                            )
                        except Exception as exc:
                            entry["value_error"] = str(exc) or repr(exc)
                    matches.append(entry)
            result: dict[str, Any] = {
                "ok": True,
                "filter": property_name_contains,
                "objects_scanned": len(summaries),
                "properties_scanned": properties_scanned,
                "matched_property_count": len(matches),
                "matches": matches,
                "targets": summaries,
                "reachability": self._reachability_note(),
            }
            if warnings:
                result["warnings"] = warnings
            if not matches:
                result["filter_matched_nothing"] = True
                result["message"] = (
                    f"在 {len(summaries)} 个可达硬件对象、{properties_scanned} 个属性名里"
                    f"没有任何名称包含 {property_name_contains}。"
                )
                result["recommendation"] = (
                    "这说明该参数不在桥接层暴露的设备/PLC/模块属性里（属于可达性边界，"
                    "不是过滤条件太窄）：请改用其它可达属性，或在 TIA Portal 中人工完成该项组态，"
                    "或升级支持更深层组态页的 siemens_tia_scripting/TIA 版本。"
                )
            return result

    @staticmethod
    def _identifier(obj: Any) -> str | None:
        getter = getattr(obj, "get_identifier", None)
        if not callable(getter):
            return None
        try:
            return str(getter())
        except Exception:
            return None

    def _resolve_hardware_target(
        self,
        target_type: str,
        target_name: str,
        device_name: str | None,
        target_identifier: str | None,
    ) -> tuple[Any, dict[str, Any]]:
        normalized_type = str(target_type or "").strip().casefold()
        if normalized_type not in {"plc", "device", "module"}:
            raise ValueError("target_type 必须是 plc、device 或 module。")
        if not isinstance(target_name, str) or not target_name.strip():
            raise ValueError("target_name 必须是非空字符串。")
        requested_name = target_name.strip().casefold()
        requested_identifier = (
            target_identifier.strip()
            if isinstance(target_identifier, str) and target_identifier.strip()
            else None
        )
        project = self._require_project()
        if normalized_type == "plc":
            candidates = list(project.get_plcs())
        elif normalized_type == "device":
            candidates = list(project.get_devices())
        else:
            devices = list(project.get_devices())
            if device_name:
                devices = [
                    device
                    for device in devices
                    if _safe_get_name(device).casefold() == device_name.casefold()
                ]
                if not devices:
                    raise RuntimeError(f"找不到设备 {device_name!r}。")
            candidates = [
                module for device in devices for module in device.get_modules()
            ]
        matches = [
            obj
            for obj in candidates
            if _safe_get_name(obj).casefold() == requested_name
            and (
                requested_identifier is None
                or self._identifier(obj) == requested_identifier
            )
        ]
        if len(matches) != 1:
            available = [
                {
                    "name": _safe_get_name(obj),
                    "identifier": self._identifier(obj),
                }
                for obj in candidates
            ]
            if not matches:
                raise RuntimeError(
                    f"找不到 {normalized_type} {target_name!r}；可选对象：{available}"
                )
            raise RuntimeError(
                f"{normalized_type} 名称 {target_name!r} 不唯一；"
                f"请传 target_identifier。候选对象：{available}"
            )
        target = matches[0]
        return target, {
            "type": normalized_type,
            "name": _safe_get_name(target),
            "identifier": self._identifier(target),
            "device_name": device_name,
        }

    @staticmethod
    def _hardware_property_value(value: Any, field: str) -> str:
        if isinstance(value, bool):
            return "True" if value else "False"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            rendered = str(value)
            if rendered.casefold() in {"nan", "inf", "-inf"}:
                raise ValueError(f"{field} 不能是 NaN 或无穷值。")
            return rendered
        if isinstance(value, str):
            if "\x00" in value:
                raise ValueError(f"{field} 不能包含 NUL 字符。")
            return value
        raise ValueError(f"{field} 只能是字符串、布尔值或数字。")

    @staticmethod
    def _hardware_property_equal(expected: str, actual: str) -> bool:
        left = str(expected).strip()
        right = str(actual).strip()
        if left.casefold() == right.casefold():
            return True
        try:
            return Decimal(left) == Decimal(right)
        except (InvalidOperation, ValueError):
            return False

    @staticmethod
    def _rollback_hardware_properties(
        target: Any, applied: list[str], before: dict[str, str]
    ) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        ok = True
        for name in reversed(applied):
            entry: dict[str, Any] = {"property": name, "restored_value": before[name]}
            try:
                entry["return_code"] = target.set_property(
                    name=name, value=before[name]
                )
                entry["readback"] = str(target.get_property(name=name))
                entry["ok"] = TiaService._hardware_property_equal(
                    before[name], entry["readback"]
                )
            except Exception as exc:
                entry.update({"ok": False, "error": str(exc) or repr(exc)})
            ok = ok and bool(entry["ok"])
            entries.append(entry)
        return {"ok": ok, "properties": entries}

    def set_hardware_properties(
        self,
        target_type: str,
        target_name: str,
        properties: dict[str, Any],
        plc_name: str | None = None,
        device_name: str | None = None,
        target_identifier: str | None = None,
        verify_readback: bool = True,
        compile_after: bool = True,
        rollback_on_error: bool = True,
        confirm_write: bool = False,
    ) -> dict[str, Any]:
        """事务式写入 Device/PLC/Module 动态硬件属性并回读核验。"""
        with self._lock:
            if not confirm_write:
                raise ValueError("写入硬件组态必须设置 confirm_write=true。")
            if not isinstance(properties, dict) or not properties:
                raise ValueError("properties 必须是非空对象。")
            target, target_info = self._resolve_hardware_target(
                target_type, target_name, device_name, target_identifier
            )
            selected_plc = (
                target
                if target_info["type"] == "plc"
                else self._require_plc(plc_name)
            )
            self._assert_offline(selected_plc)

            getter = getattr(target, "get_property", None)
            setter = getattr(target, "set_property", None)
            names_getter = getattr(target, "get_properties", None)
            if not callable(getter) or not callable(setter) or not callable(names_getter):
                raise RuntimeError("目标对象不支持 Openness 动态属性读写。")
            available_names = [str(name) for name in names_getter()]
            by_casefold = {name.casefold(): name for name in available_names}
            missing = [
                name
                for name in properties
                if not isinstance(name, str) or name.casefold() not in by_casefold
            ]
            if missing:
                hints = self._property_location_hint(missing)
                if hints:
                    location = "；".join(
                        f"{hint['target_type']} {hint['name']!r} 暴露 {hint['property_names']}"
                        for hint in hints
                    )
                    raise RuntimeError(
                        f"目标对象不暴露硬件属性 {missing}；这些名称实际出现在其它可达对象："
                        f"{location}。请改用对应的 target_type / target_name / device_name 重试。"
                    )
                raise RuntimeError(
                    f"目标对象不暴露硬件属性 {missing}；"
                    "请先用 get_hardware_inventory 或 find_hardware_property 读取实际属性名。"
                )
            requested: dict[str, str] = {}
            for supplied_name, value in properties.items():
                canonical = by_casefold[supplied_name.casefold()]
                requested[canonical] = self._hardware_property_value(
                    value, f"properties.{supplied_name}"
                )
            before: dict[str, str] = {}
            for name in requested:
                try:
                    before[name] = str(getter(name=name))
                except Exception as exc:
                    raise RuntimeError(
                        f"无法读取硬件属性 {name!r} 的原值；为保证可回滚，"
                        "未写入任何属性。"
                    ) from exc
            applied: list[str] = []
            changes: list[dict[str, Any]] = []
            failure: Exception | None = None
            for name, value in requested.items():
                entry: dict[str, Any] = {
                    "property": name,
                    "before": before[name],
                    "requested": value,
                }
                try:
                    # 即使底层 setter 在部分写入后抛异常，也要尝试恢复该属性。
                    applied.append(name)
                    entry["return_code"] = setter(name=name, value=value)
                    entry["readback"] = str(getter(name=name))
                    entry["verified"] = self._hardware_property_equal(
                        value, entry["readback"]
                    )
                    if verify_readback and not entry["verified"]:
                        raise RuntimeError(
                            f"属性 {name} 写后回读不一致：请求 {value!r}，"
                            f"实际 {entry['readback']!r}。"
                        )
                except Exception as exc:
                    entry["error"] = str(exc) or repr(exc)
                    failure = exc
                changes.append(entry)
                if failure is not None:
                    break

            rollback = None
            if failure is not None:
                if rollback_on_error:
                    rollback = self._rollback_hardware_properties(
                        target, applied, before
                    )
                return user_actions.attach(
                    {
                        "ok": False,
                        "target": target_info,
                        "changes": changes,
                        "error": str(failure) or repr(failure),
                        "rollback": rollback,
                        "compiled": False,
                        "saved": False,
                    },
                    str(failure),
                )

            compilation = None
            if compile_after:
                compilation = self.compile_plc(
                    "hardware", str(selected_plc.get_name())
                )
                if not compilation.get("ok"):
                    if rollback_on_error:
                        rollback = self._rollback_hardware_properties(
                            target, applied, before
                        )
                        rollback["compile"] = self.compile_plc(
                            "hardware", str(selected_plc.get_name())
                        )
                    return user_actions.attach(
                        {
                            "ok": False,
                            "target": target_info,
                            "changes": changes,
                            "error": "硬件编译失败。",
                            "compile": compilation,
                            "rollback": rollback,
                            "saved": False,
                        },
                        compilation.get("hardware"),
                        compilation.get("software"),
                    )
            result = {
                "ok": True,
                "target": target_info,
                "changes": changes,
                "compile": compilation,
                "saved": False,
            }
            if isinstance(compilation, dict):
                return user_actions.attach(
                    result,
                    compilation.get("hardware"),
                    compilation.get("software"),
                )
            return result

    def configure_s71200_hsc(
        self,
        target_type: str,
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
        """配置 S7-1200 HSC；型号未知时从真实动态属性中解析映射。"""
        with self._lock:
            if not isinstance(hsc_number, int) or isinstance(hsc_number, bool) or not 1 <= hsc_number <= 8:
                raise ValueError("hsc_number 必须是 1 到 8 的整数。")
            if not isinstance(clock_input, str) or not re.fullmatch(
                r"%?I\d+\.\d+", clock_input.strip(), flags=re.IGNORECASE
            ):
                raise ValueError("clock_input 必须是 I0.0 或 %I0.0 一类数字输入地址。")
            target, target_info = self._resolve_hardware_target(
                target_type, target_name, device_name, target_identifier
            )
            names, names_error = self._property_scan(target)
            if names_error:
                raise RuntimeError(
                    f"无法枚举目标对象的 Openness 动态属性：{names_error}"
                ) from None
            values = _safe_properties(target)
            by_casefold = {name.casefold(): name for name in names}
            mapping = dict(property_map or {})
            unknown_mapping_keys = set(mapping) - {"enabled", "clock_input"}
            if unknown_mapping_keys:
                raise ValueError(
                    f"property_map 仅支持 enabled 和 clock_input：{sorted(unknown_mapping_keys)}"
                )
            for logical_name, supplied_name in list(mapping.items()):
                if not isinstance(supplied_name, str) or supplied_name.casefold() not in by_casefold:
                    hints = self._property_location_hint(
                        [supplied_name] if isinstance(supplied_name, str) else []
                    )
                    location = (
                        "；".join(
                            f"{hint['target_type']} {hint['name']!r} 暴露 {hint['property_names']}"
                            for hint in hints
                        )
                        if hints
                        else "目标对象和其它可达对象都没有这个属性名"
                    )
                    raise RuntimeError(
                        f"property_map.{logical_name}={supplied_name!r} 不是目标对象属性；"
                        f"{location}。可用 find_hardware_property 扫描可达属性名。"
                    )
                mapping[logical_name] = by_casefold[supplied_name.casefold()]

            normalized = {name: self._compact_hardware_name(name) for name in names}
            hsc_tokens = (
                f"hsc{hsc_number}",
                f"highspeedcounter{hsc_number}",
                f"highspeedcount{hsc_number}",
            )
            hsc_names = [
                name
                for name, compact in normalized.items()
                if any(token in compact for token in hsc_tokens)
            ]
            role_markers = {
                "enabled": ("enable", "enabled", "active", "activate"),
                "clock_input": (
                    "clockinput",
                    "countinput",
                    "inputa",
                    "digitalinput",
                    "clockgeneratorforward",
                ),
            }
            role_candidates: dict[str, list[str]] = {}
            for role, markers in role_markers.items():
                candidates = [
                    name
                    for name in hsc_names
                    if any(marker in normalized[name] for marker in markers)
                ]
                role_candidates[role] = candidates
                if role not in mapping and len(candidates) == 1:
                    mapping[role] = candidates[0]
            missing = {"enabled", "clock_input"} - set(mapping)
            if missing:
                near_miss = [
                    name
                    for name in names
                    if any(
                        marker in normalized[name]
                        for marker in ("counter", "clock", "input")
                    )
                ]
                suggestions = hsc_names or near_miss
                reachable_targets, _warnings = self._hardware_targets()
                diagnosis: dict[str, Any] = {
                    "ok": False,
                    "target": target_info,
                    "missing_mapping": sorted(missing),
                    "role_candidates": role_candidates,
                    "candidate_property_names": suggestions[:100],
                    "candidate_properties": {
                        name: values.get(name) for name in suggestions[:100]
                    },
                    "objects_reachable": [
                        self._public_target_summary(item)
                        for item in reachable_targets
                    ],
                }
                if hsc_names:
                    # 目标对象上确有 HSC 属性，只是角色无法唯一判定：这才是“需要映射”。
                    diagnosis.update(
                        {
                            "needs_property_map": True,
                            "hsc_properties_reachable": True,
                            "hsc_property_names": hsc_names,
                            "message": (
                                "目标对象上存在 HSC 属性，但 enabled / clock_input 无法唯一"
                                "确定；请核对 role_candidates，用 property_map.enabled 和 "
                                "property_map.clock_input 传回；未写入工程。"
                            ),
                        }
                    )
                    return diagnosis
                elsewhere = self._token_property_targets(hsc_tokens)
                if elsewhere:
                    first = elsewhere[0]
                    diagnosis.update(
                        {
                            "needs_property_map": True,
                            "hsc_properties_reachable": True,
                            "hsc_properties_found_elsewhere": elsewhere,
                            "retry": {
                                "target_type": first.get("target_type"),
                                "target_name": first.get("name"),
                                "target_identifier": first.get("identifier"),
                                "device_name": first.get("device_name"),
                                "confirm_write": True,
                            },
                            "message": (
                                f"目标对象（{target_info['type']} {target_info['name']!r}）"
                                "没有 HSC 属性，但在其它可达对象上找到了；请按 retry 指定的"
                                "对象重试；未写入工程。"
                            ),
                        }
                    )
                    return diagnosis
                diagnosis.update(
                    {
                        "needs_property_map": False,
                        "hsc_properties_reachable": False,
                        "message": (
                            f"未在任何可达硬件对象上找到 HSC{hsc_number} 相关属性；"
                            "这不是属性名映射问题，而是桥接层的可达性边界："
                            "siemens_tia_scripting 只在 Device 上提供 get_modules()，"
                            "CPU 下更深层的组态页不在枚举范围内。未写入工程。"
                        ),
                        "recommendation": (
                            "请在 TIA Portal 中人工组态该高速计数器（CPU 属性 → 高速计数器），"
                            "或升级支持该组态页的 siemens_tia_scripting/TIA 版本；"
                            "本工具不会猜测或硬编码属性名。可用 find_hardware_property 复核可达属性。"
                        ),
                    }
                )
                return diagnosis
            if mapping["enabled"] == mapping["clock_input"]:
                raise ValueError("enabled 与 clock_input 不能映射到同一硬件属性。")
            result = self.set_hardware_properties(
                target_type=target_type,
                target_name=target_name,
                properties={
                    mapping["enabled"]: enabled,
                    mapping["clock_input"]: clock_input.strip(),
                },
                plc_name=plc_name,
                device_name=device_name,
                target_identifier=target_identifier,
                verify_readback=True,
                compile_after=compile_after,
                rollback_on_error=True,
                confirm_write=confirm_write,
            )
            result["hsc"] = {
                "number": hsc_number,
                "enabled": enabled,
                "clock_input": clock_input.strip(),
                "property_map": mapping,
            }
            return result

    def plc_inventory(
        self,
        plc_name: str | None = None,
        include_tag_entries: bool = False,
        include_properties: bool = False,
    ) -> dict[str, Any]:
        """读取指定 PLC 中的各类工程对象。"""
        with self._lock:
            plc = self._require_plc(plc_name)
            blocks = []
            for block in plc.get_program_blocks():
                item = _object_summary(block, include_properties)
                try:
                    item["consistent"] = bool(block.is_consistent())
                except Exception:
                    pass
                blocks.append(item)

            tag_tables = []
            for table in plc.get_plc_tag_tables():
                item = _object_summary(table, include_properties)
                if include_tag_entries:
                    item["tags"] = [
                        _object_summary(tag, include_properties)
                        for tag in table.get_plc_tags()
                    ]
                    item["user_constants"] = [
                        _object_summary(value, include_properties)
                        for value in table.get_user_constants()
                    ]
                tag_tables.append(item)

            def summaries(method_name: str) -> list[dict[str, Any]]:
                """读取指定类型的 PLC 对象摘要。"""
                method = getattr(plc, method_name)
                return [
                    _object_summary(item, include_properties) for item in method()
                ]

            return {
                "ok": True,
                "plc": _object_summary(plc, include_properties),
                "online_state": str(plc.get_online_state()),
                "program_blocks": blocks,
                "plc_tag_tables": tag_tables,
                "udts": summaries("get_user_data_types"),
                "software_units": summaries("get_software_units"),
                "external_sources": summaries("get_external_sources"),
                "watch_tables": summaries("get_watch_tables"),
                "force_tables": summaries("get_force_tables"),
                "technology_objects": summaries("get_technology_objects"),
            }

    @staticmethod
    def _assert_offline(plc: Any) -> None:
        """确认 PLC 处于离线状态后才允许修改。"""
        state = str(plc.get_online_state())
        if state.strip().casefold() != "offline":
            raise RuntimeError(
                f"PLC 当前状态为 {state!r}。修改工程前请先调用 go_offline。"
            )

    def normalize_online_target(self, target: str) -> str:
        """校验 go_online/download 的第三参数（V21 为 PLC 接口名）。"""
        version = self.connected_tia_version()
        value = str(target or "").strip()
        if not value:
            raise ValueError(
                f"{version.label} 必须提供 PLC 目标接口名，例如 '1 X1'。"
            )
        try:
            ipaddress.ip_address(value)
        except ValueError:
            return value
        raise ValueError(
            f"{version.label} 的 target_interface 参数必须填写 PLC 接口名"
            f"（例如 '1 X1'），不能填写 IP 地址 {value!r}；"
            "V21 的在线目标已改为设备接口名。"
        )

    def go_online(
        self,
        pc_interface_type: str,
        pc_interface_name: str,
        target_interface: str,
        plc_name: str | None = None,
    ) -> dict[str, Any]:
        """通过指定接口将 PLC 工程对象转至在线。"""
        with self._lock:
            plc = self._require_plc(plc_name)
            target_interface = self.normalize_online_target(target_interface)
            try:
                returned = plc.go_online(
                    pc_interface_type=pc_interface_type,
                    pc_interface_name=pc_interface_name,
                    target_interface=target_interface,
                )
            except ValueError as exc:
                failed = {
                    "ok": False,
                    "plc": str(plc.get_name()),
                    "error_code": "online_configuration_failed",
                    "error": str(exc),
                    "hint": (
                        "请在 TIA Portal 的转至在线窗口核对 PN/IE 下的 PG/PC 接口名"
                        "及 PLC 目标接口；仿真连接还需先启动 PLCSIM。"
                    ),
                }
                return user_actions.attach(failed, str(exc))
            return {
                "ok": "offline" not in str(plc.get_online_state()).casefold(),
                "plc": str(plc.get_name()),
                "result": str(returned),
                "online_state": str(plc.get_online_state()),
            }

    def go_offline(self, plc_name: str | None = None) -> dict[str, Any]:
        """将 PLC 工程对象转至离线。"""
        with self._lock:
            plc = self._require_plc(plc_name)
            before = str(plc.get_online_state())
            if before.strip().casefold() != "offline":
                plc.go_offline()
            after = str(plc.get_online_state())
            return {
                "ok": after.strip().casefold() == "offline",
                "plc": str(plc.get_name()),
                "before": before,
                "after": after,
            }

    @staticmethod
    def _select_named(objects: Sequence[Any], names: list[str] | None) -> list[Any]:
        """按名称从对象集合中选择目标。"""
        if names is None:
            return list(objects)
        if not names:
            raise ValueError("名称列表不能为空；传 null 表示全部对象。")
        by_name = {_safe_get_name(obj).casefold(): obj for obj in objects}
        missing = [name for name in names if name.casefold() not in by_name]
        if missing:
            raise RuntimeError(f"找不到对象：{missing}；现有对象：{list(by_name)}")
        return [by_name[name.casefold()] for name in names]

    def _export_objects(
        self,
        objects: Sequence[Any],
        target_directory: str | Path,
        export_format: str = "simaticml",
    ) -> dict[str, Any]:
        """将 TIA 对象导出到指定目录。"""
        ts = self._library()
        destination = output_path(target_directory, "target_directory")
        destination.mkdir(parents=True, exist_ok=True)
        normalized = export_format.casefold().replace("_", "-")
        if normalized in {"simaticsd", "simatic-sd"}:
            self.require_simatic_sd_lad()
        format_names = {
            "simaticml": "SimaticML",
            "xml": "SimaticML",
            "external-source": "ExternalSource",
            "source": "ExternalSource",
            "simaticsd": "SimaticSD",
            "simatic-sd": "SimaticSD",
        }
        if normalized not in format_names:
            raise ValueError(
                "export_format 必须是 simaticml、external-source 或 simaticsd。"
            )
        native_format = getattr(
            ts.Enums.GeneralExportFormats, format_names[normalized], None
        )
        if native_format is None:
            raise RuntimeError(
                f"{self.connected_tia_version().label} 的 Openness 适配器不支持导出格式 "
                f"{normalized!r}。"
            )
        before = {str(path) for path in destination.rglob("*") if path.is_file()}
        exported = []
        for obj in objects:
            obj.export(
                target_directory_path=str(destination),
                export_options=ts.Enums.GeneralExportOptions.WithDefaults,
                export_format=native_format,
                keep_folder_structure=True,
            )
            exported.append(_safe_get_name(obj))
        after = {str(path) for path in destination.rglob("*") if path.is_file()}
        return {
            "ok": True,
            "target_directory": str(destination),
            "exported": exported,
            "created_files": sorted(after - before),
        }

    def export_program_blocks(
        self,
        target_directory: str,
        block_names: list[str] | None = None,
        export_format: str = "simaticml",
        plc_name: str | None = None,
    ) -> dict[str, Any]:
        """导出指定 PLC 的程序块。"""
        with self._lock:
            plc = self._require_plc(plc_name)
            objects = self._select_named(list(plc.get_program_blocks()), block_names)
            return self._export_objects(objects, target_directory, export_format)

    def export_plc_tag_tables(
        self,
        target_directory: str,
        table_names: list[str] | None = None,
        plc_name: str | None = None,
    ) -> dict[str, Any]:
        """导出指定 PLC 的变量表。"""
        with self._lock:
            plc = self._require_plc(plc_name)
            objects = self._select_named(list(plc.get_plc_tag_tables()), table_names)
            return self._export_objects(objects, target_directory, "simaticml")

    def export_udts(
        self,
        target_directory: str,
        udt_names: list[str] | None = None,
        plc_name: str | None = None,
    ) -> dict[str, Any]:
        """导出指定 PLC 的用户数据类型。"""
        with self._lock:
            plc = self._require_plc(plc_name)
            objects = self._select_named(list(plc.get_user_data_types()), udt_names)
            return self._export_objects(objects, target_directory, "simaticml")

    def export_watch_tables(
        self,
        target_directory: str,
        table_names: list[str] | None = None,
        plc_name: str | None = None,
    ) -> dict[str, Any]:
        """导出指定 PLC 的监控表。"""
        with self._lock:
            plc = self._require_plc(plc_name)
            objects = self._select_named(list(plc.get_watch_tables()), table_names)
            return self._export_objects(objects, target_directory, "simaticml")

    def get_technology_object_details(
        self,
        object_names: list[str] | None = None,
        plc_name: str | None = None,
        include_properties: bool = True,
        include_fingerprints: bool = False,
    ) -> dict[str, Any]:
        """读取工艺对象的属性、路径、一致性和可用导出格式。"""
        with self._lock:
            plc = self._require_plc(plc_name)
            objects = self._select_named(
                list(plc.get_technology_objects()), object_names
            )
            details: list[dict[str, Any]] = []
            for obj in objects:
                item = _object_summary(obj, include_properties)
                consistent = getattr(obj, "is_consistent", None)
                if callable(consistent):
                    try:
                        item["consistent"] = bool(consistent())
                    except Exception as exc:
                        item["consistent_error"] = str(exc) or repr(exc)
                formats = getattr(obj, "get_supported_export_format", None)
                if callable(formats):
                    try:
                        item["supported_export_formats"] = [
                            str(value) for value in (formats() or [])
                        ]
                    except Exception as exc:
                        item["supported_export_formats_error"] = str(exc) or repr(exc)
                if include_fingerprints:
                    fingerprints = getattr(obj, "get_fingerprints", None)
                    if callable(fingerprints):
                        try:
                            item["fingerprints"] = [
                                [str(name), str(value)]
                                for name, value in (fingerprints() or [])
                            ]
                        except Exception as exc:
                            item["fingerprints_error"] = str(exc) or repr(exc)
                details.append(item)
            return {
                "ok": True,
                "plc": str(plc.get_name()),
                "technology_objects": details,
                "count": len(details),
            }

    def export_technology_objects(
        self,
        target_directory: str,
        object_names: list[str] | None = None,
        plc_name: str | None = None,
    ) -> dict[str, Any]:
        """把指定工艺对象导出为可复用的同版本 SimaticML 模板。"""
        with self._lock:
            plc = self._require_plc(plc_name)
            objects = self._select_named(
                list(plc.get_technology_objects()), object_names
            )
            result = self._export_objects(objects, target_directory, "simaticml")
            result["plc"] = str(plc.get_name())
            result["template_note"] = (
                "导出的工艺对象应在相同或兼容的 TIA/CPU 环境中回导；"
                "硬件引用和工艺对象类型仍受目标 CPU 支持范围约束。"
            )
            return result

    @contextmanager
    def _import_root(self, source_path: str) -> Iterator[Path]:
        """为单文件或目录导入准备隔离的根目录。"""
        source = absolute_path(source_path, "source_path")
        if source.is_dir():
            yield source
            return
        if not source.is_file():
            raise FileNotFoundError(f"找不到导入文件或目录：{source}")
        with tempfile.TemporaryDirectory(prefix="tia_mcp_import_") as temp:
            staged = Path(temp) / source.name
            shutil.copy2(source, staged)
            yield Path(temp)

    def _import_collection(
        self,
        method_name: str,
        source_path: str,
        target_folder_path: str | None,
        plc_name: str | None,
        confirm_import: bool,
    ) -> dict[str, Any]:
        """在确认且离线后导入指定类型的 PLC 对象。"""
        if not confirm_import:
            raise ValueError("写入 TIA 工程必须设置 confirm_import=true。")
        plc = self._require_plc(plc_name)
        self._assert_offline(plc)
        ts = self._library()
        method = getattr(plc, method_name)
        with self._import_root(source_path) as root:
            self._assert_import_root_compatible(root, method_name)
            kwargs: dict[str, Any] = {
                "import_root_directory": str(root),
                "import_options": ts.Enums.GeneralImportOptions.Override,
            }
            if target_folder_path:
                kwargs["target_folder_path"] = target_folder_path
            method(**kwargs)
        return {
            "ok": True,
            "plc": str(plc.get_name()),
            "source_path": str(absolute_path(source_path, "source_path")),
            "target_folder_path": target_folder_path,
            "operation": method_name,
            "saved": False,
        }

    def _assert_import_root_compatible(self, root: Path, method_name: str) -> None:
        """对即将导入的目录做版本预检，避免让 Portal 处理危险降版本文件。"""
        version = self.connected_tia_version()
        xml_files = sorted(root.rglob("*.xml"))
        for path in xml_files:
            try:
                text = path.read_text(encoding="utf-8-sig")
            except UnicodeError as exc:
                raise ValueError(f"无法以 UTF-8 读取 SimaticML XML：{path}") from exc
            assert_xml_import_compatible(text, version)
        if method_name == "import_blocks" and any(root.rglob("*.s7dcl")):
            self.require_simatic_sd_lad()

    def import_program_blocks(
        self,
        source_path: str,
        target_folder_path: str | None = None,
        plc_name: str | None = None,
        confirm_import: bool = False,
    ) -> dict[str, Any]:
        """从文件或目录导入程序块。"""
        with self._lock:
            return self._import_collection(
                "import_blocks",
                source_path,
                target_folder_path,
                plc_name,
                confirm_import,
            )

    def import_plc_tag_tables(
        self,
        source_path: str,
        target_folder_path: str | None = None,
        plc_name: str | None = None,
        confirm_import: bool = False,
    ) -> dict[str, Any]:
        """从文件或目录导入 PLC 变量表。"""
        with self._lock:
            return self._import_collection(
                "import_plc_tags",
                source_path,
                target_folder_path,
                plc_name,
                confirm_import,
            )

    def import_udts(
        self,
        source_path: str,
        target_folder_path: str | None = None,
        plc_name: str | None = None,
        confirm_import: bool = False,
    ) -> dict[str, Any]:
        """从文件或目录导入用户数据类型。"""
        with self._lock:
            return self._import_collection(
                "import_data_types",
                source_path,
                target_folder_path,
                plc_name,
                confirm_import,
            )

    def import_watch_tables(
        self,
        source_path: str,
        target_folder_path: str | None = None,
        plc_name: str | None = None,
        confirm_import: bool = False,
    ) -> dict[str, Any]:
        """从文件或目录导入监控表。"""
        with self._lock:
            return self._import_collection(
                "import_watch_tables",
                source_path,
                target_folder_path,
                plc_name,
                confirm_import,
            )

    def import_technology_objects(
        self,
        source_path: str,
        target_folder_path: str | None = None,
        plc_name: str | None = None,
        confirm_import: bool = False,
    ) -> dict[str, Any]:
        """从同版本 SimaticML 模板创建或覆盖工艺对象。"""
        with self._lock:
            plc = self._require_plc(plc_name)
            before = {
                _safe_get_name(obj).casefold(): _safe_get_name(obj)
                for obj in plc.get_technology_objects()
            }
            result = self._import_collection(
                "import_technology_objects",
                source_path,
                target_folder_path,
                plc_name,
                confirm_import,
            )
            after_objects = list(plc.get_technology_objects())
            after = {
                _safe_get_name(obj).casefold(): _safe_get_name(obj)
                for obj in after_objects
            }
            result.update({
                "technology_objects": sorted(after.values()),
                "new_objects": sorted(
                    name for key, name in after.items() if key not in before
                ),
                "existing_object_count_before": len(before),
                "object_count_after": len(after),
                "creation_method": "template_import",
            })
            return result

    def write_program_blocks(
        self,
        source_text: str,
        source_file_name: str = "mcp_source.scl",
        target_folder_path: str | None = None,
        plc_name: str | None = None,
        allow_overwrite: bool = False,
        cleanup_external_source: bool = True,
        confirm_write: bool = False,
    ) -> dict[str, Any]:
        """将 SCL、SIMATIC SD 或 XML 文本写入 PLC 程序块。"""
        with self._lock:
            if not confirm_write:
                raise ValueError("写入程序块必须设置 confirm_write=true。")
            if not source_text.strip():
                raise ValueError("source_text 不能为空。")
            file_name = Path(source_file_name).name
            if file_name != source_file_name or Path(file_name).suffix.casefold() not in {
                ".scl",
                ".s7dcl",
                ".xml",
            }:
                raise ValueError("source_file_name 只能是 .scl、.s7dcl 或 .xml 文件名。")

            suffix = Path(file_name).suffix.casefold()
            version = self.connected_tia_version()
            if suffix == ".s7dcl":
                self.require_simatic_sd_lad()
            elif suffix == ".xml":
                assert_xml_import_compatible(source_text, version)

            plc = self._require_plc(plc_name)
            self._assert_offline(plc)
            ts = self._library()
            before = {
                str(block.get_name()).casefold(): block
                for block in plc.get_program_blocks()
            }
            declared = (
                declared_scl_blocks(source_text)
                if suffix in {".scl", ".s7dcl"}
                else []
            )
            if suffix == ".s7dcl" and (
                len(declared) != 1 or declared[0].casefold() != Path(file_name).stem.casefold()
            ):
                raise ValueError(".s7dcl 必须声明一个与文件名相同的程序块。")
            conflicts = [name for name in declared if name.casefold() in before]
            if conflicts and not allow_overwrite:
                raise RuntimeError(
                    f"以下程序块已存在：{conflicts}。如需覆盖请设置 allow_overwrite=true。"
                )

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            staged_name = (
                file_name
                if suffix == ".s7dcl"
                else f"MCP_{Path(file_name).stem}_{stamp}{Path(file_name).suffix}"
            )
            source_hash = hashlib.sha256(source_text.encode("utf-8-sig")).hexdigest()
            with tempfile.TemporaryDirectory(prefix="tia_mcp_write_") as temp:
                staged_path = Path(temp) / staged_name
                staged_path.write_text(source_text, encoding="utf-8-sig", newline="\n")
                kwargs: dict[str, Any] = {
                    "import_root_directory": str(Path(temp)),
                    "import_options": ts.Enums.GeneralImportOptions.Override,
                }
                if target_folder_path:
                    kwargs["target_folder_path"] = target_folder_path
                plc.import_blocks(**kwargs)

            generated_from_source = False
            imported_sources = [
                source
                for source in plc.get_external_sources()
                if str(source.get_name()).casefold()
                == Path(staged_name).stem.casefold()
            ]
            after = {
                str(block.get_name()).casefold(): block
                for block in plc.get_program_blocks()
            }
            missing = [name for name in declared if name.casefold() not in after]
            if missing and imported_sources:
                for source in imported_sources:
                    source.block_gen()
                generated_from_source = True
                after = {
                    str(block.get_name()).casefold(): block
                    for block in plc.get_program_blocks()
                }
                missing = [name for name in declared if name.casefold() not in after]
            if missing:
                hint = ""
                if suffix == ".s7dcl":
                    hint = (
                        " TIA 没有根据 .s7dcl 生成程序块。常见原因：使用了绝对地址"
                        "（如 %M0.0，SIMATIC SD 需要 PLC 符号变量）、块声明与文件名不一致，"
                        "或 SD 语法不被当前 V21 接受。"
                    )
                raise RuntimeError(
                    f"导入完成后仍缺少声明的程序块：{missing}。" + hint
                )

            cleaned_sources: list[str] = []
            if cleanup_external_source:
                for source in imported_sources:
                    name = str(source.get_name())
                    source.delete()
                    cleaned_sources.append(name)

            new_names = [
                str(block.get_name())
                for key, block in after.items()
                if key not in before
            ]
            return {
                "ok": True,
                "plc": str(plc.get_name()),
                "declared_blocks": declared,
                "new_blocks": sorted(new_names),
                "overwritten_blocks": conflicts,
                "generated_from_external_source": generated_from_source,
                "cleaned_external_sources": cleaned_sources,
                "source_sha256": source_hash,
                "saved": False,
            }

    def compile_technology_objects(
        self,
        object_names: list[str] | None = None,
        plc_name: str | None = None,
    ) -> dict[str, Any]:
        """逐个编译工艺对象并返回对象级诊断。"""
        with self._lock:
            plc = self._require_plc(plc_name)
            self._assert_offline(plc)
            objects = self._select_named(
                list(plc.get_technology_objects()), object_names
            )
            results: list[dict[str, Any]] = []
            ok = True
            for obj in objects:
                item: dict[str, Any] = {"name": _safe_get_name(obj)}
                try:
                    item["compile"] = execution_result_to_dict(obj.compile())
                    try:
                        item["consistent"] = bool(obj.is_consistent())
                    except Exception as exc:
                        item["consistent_error"] = str(exc) or repr(exc)
                    item["ok"] = bool(item["compile"]["ok"]) and item.get(
                        "consistent", True
                    )
                except Exception as exc:
                    item.update({
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc) or repr(exc),
                    })
                ok = ok and bool(item["ok"])
                results.append(item)
            return {
                "ok": ok,
                "plc": str(plc.get_name()),
                "technology_objects": results,
                "compiled_count": len(results),
                "saved": False,
            }

    def set_technology_object_properties(
        self,
        object_name: str,
        properties: dict[str, Any],
        plc_name: str | None = None,
        verify_readback: bool = True,
        compile_after: bool = True,
        rollback_on_error: bool = True,
        confirm_write: bool = False,
    ) -> dict[str, Any]:
        """事务式修改单个工艺对象已由 Openness 暴露的动态属性。"""
        with self._lock:
            if not confirm_write:
                raise ValueError("修改工艺对象必须设置 confirm_write=true。")
            if not isinstance(properties, dict) or not properties:
                raise ValueError("properties 必须是非空对象。")
            plc = self._require_plc(plc_name)
            self._assert_offline(plc)
            selected = self._select_named(
                list(plc.get_technology_objects()), [object_name]
            )
            obj = selected[0]
            getter = getattr(obj, "get_property", None)
            setter = getattr(obj, "set_property", None)
            names_getter = getattr(obj, "get_properties", None)
            if not callable(getter) or not callable(setter) or not callable(names_getter):
                raise RuntimeError("目标工艺对象不支持 Openness 动态属性读写。")

            available_names = [str(name) for name in names_getter()]
            by_casefold = {name.casefold(): name for name in available_names}
            missing = [
                name
                for name in properties
                if not isinstance(name, str) or name.casefold() not in by_casefold
            ]
            if missing:
                raise RuntimeError(
                    f"工艺对象 {object_name!r} 不暴露属性 {missing}；"
                    "请先调用 get_technology_object_details(include_properties=true) "
                    "读取该对象实际公开的属性名。"
                )

            requested: dict[str, str] = {}
            for supplied_name, value in properties.items():
                canonical = by_casefold[supplied_name.casefold()]
                requested[canonical] = self._hardware_property_value(
                    value, f"properties.{supplied_name}"
                )
            before: dict[str, str] = {}
            for name in requested:
                try:
                    before[name] = str(getter(name=name))
                except Exception as exc:
                    raise RuntimeError(
                        f"无法读取工艺对象属性 {name!r} 的原值；为保证可回滚，"
                        "未写入任何属性。"
                    ) from exc

            applied: list[str] = []
            changes: list[dict[str, Any]] = []
            failure: Exception | None = None
            for name, value in requested.items():
                entry: dict[str, Any] = {
                    "property": name,
                    "before": before[name],
                    "requested": value,
                }
                try:
                    # setter 可能在部分写入后才抛异常，因此先登记为待回滚。
                    applied.append(name)
                    entry["return_code"] = setter(name=name, value=value)
                    entry["readback"] = str(getter(name=name))
                    entry["verified"] = self._hardware_property_equal(
                        value, entry["readback"]
                    )
                    if verify_readback and not entry["verified"]:
                        raise RuntimeError(
                            f"工艺对象属性 {name} 写后回读不一致：请求 {value!r}，"
                            f"实际 {entry['readback']!r}。"
                        )
                except Exception as exc:
                    entry["error"] = str(exc) or repr(exc)
                    failure = exc
                changes.append(entry)
                if failure is not None:
                    break

            rollback = None
            if failure is not None:
                if rollback_on_error:
                    rollback = self._rollback_hardware_properties(obj, applied, before)
                return {
                    "ok": False,
                    "plc": str(plc.get_name()),
                    "technology_object": _safe_get_name(obj),
                    "changes": changes,
                    "error": str(failure) or repr(failure),
                    "rollback": rollback,
                    "compiled": False,
                    "saved": False,
                }

            compilation = None
            if compile_after:
                consistent = None
                try:
                    compilation = execution_result_to_dict(obj.compile())
                    try:
                        consistent = bool(obj.is_consistent())
                    except Exception:
                        pass
                    compile_ok = bool(compilation["ok"]) and consistent is not False
                except Exception as exc:
                    compilation = {
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc) or repr(exc),
                    }
                    compile_ok = False
                if not compile_ok:
                    if rollback_on_error:
                        rollback = self._rollback_hardware_properties(
                            obj, applied, before
                        )
                        try:
                            rollback["compile"] = execution_result_to_dict(obj.compile())
                        except Exception as exc:
                            rollback["compile_error"] = str(exc) or repr(exc)
                    return {
                        "ok": False,
                        "plc": str(plc.get_name()),
                        "technology_object": _safe_get_name(obj),
                        "changes": changes,
                        "error": "工艺对象编译失败或对象不一致。",
                        "compile": compilation,
                        "consistent": consistent,
                        "rollback": rollback,
                        "saved": False,
                    }
            return {
                "ok": True,
                "plc": str(plc.get_name()),
                "technology_object": _safe_get_name(obj),
                "changes": changes,
                "compile": compilation,
                "saved": False,
            }

    def delete_technology_objects(
        self,
        object_names: list[str],
        plc_name: str | None = None,
        backup_before_delete: bool = True,
        backup_directory: str | None = None,
        confirm_delete: bool = False,
    ) -> dict[str, Any]:
        """确认并备份后删除指定工艺对象。"""
        with self._lock:
            if not confirm_delete:
                raise ValueError("删除工艺对象必须设置 confirm_delete=true。")
            plc = self._require_plc(plc_name)
            self._assert_offline(plc)
            selected = self._select_named(
                list(plc.get_technology_objects()), object_names
            )
            backup_result = None
            if backup_before_delete:
                if not backup_directory:
                    raise ValueError(
                        "backup_before_delete=true 时必须指定 backup_directory；"
                        "服务不会在自身目录自动保存备份。"
                    )
                target = output_path(backup_directory, "backup_directory")
                backup_result = self._export_objects(selected, target, "simaticml")
            deleted: list[str] = []
            for obj in selected:
                name = _safe_get_name(obj)
                obj.delete()
                deleted.append(name)
            remaining = {
                _safe_get_name(obj).casefold()
                for obj in plc.get_technology_objects()
            }
            failed = [name for name in deleted if name.casefold() in remaining]
            if failed:
                raise RuntimeError(f"删除后工艺对象仍存在：{failed}")
            return {
                "ok": True,
                "plc": str(plc.get_name()),
                "deleted": deleted,
                "backup": backup_result,
                "saved": False,
            }

    def delete_program_blocks(
        self,
        block_names: list[str],
        plc_name: str | None = None,
        backup_before_delete: bool = True,
        backup_directory: str | None = None,
        confirm_delete: bool = False,
    ) -> dict[str, Any]:
        """确认并备份后删除指定程序块。"""
        with self._lock:
            if not confirm_delete:
                raise ValueError("删除程序块必须设置 confirm_delete=true。")
            plc = self._require_plc(plc_name)
            self._assert_offline(plc)
            selected = self._select_named(list(plc.get_program_blocks()), block_names)
            backup_result = None
            if backup_before_delete:
                if not backup_directory:
                    raise ValueError(
                        "backup_before_delete=true 时必须指定 backup_directory；"
                        "服务不会在自身目录自动保存备份。"
                    )
                target = output_path(backup_directory, "backup_directory")
                backup_result = self._export_objects(selected, target, "simaticml")
            deleted = []
            for block in selected:
                name = str(block.get_name())
                block.delete()
                deleted.append(name)
            remaining = {
                str(block.get_name()).casefold()
                for block in plc.get_program_blocks()
            }
            failed = [name for name in deleted if name.casefold() in remaining]
            if failed:
                raise RuntimeError(f"删除后程序块仍存在：{failed}")
            return {
                "ok": True,
                "plc": str(plc.get_name()),
                "deleted": deleted,
                "backup": backup_result,
                "saved": False,
            }

    def compile_plc(
        self, scope: str = "both", plc_name: str | None = None
    ) -> dict[str, Any]:
        """编译 PLC 硬件、软件或两者。"""
        with self._lock:
            plc = self._require_plc(plc_name)
            self._assert_offline(plc)
            normalized = scope.strip().casefold()
            if normalized not in {"hardware", "software", "both"}:
                raise ValueError("scope 必须是 hardware、software 或 both。")
            result: dict[str, Any] = {
                "ok": True,
                "plc": str(plc.get_name()),
                "scope": normalized,
            }
            if normalized in {"hardware", "both"}:
                hardware = execution_result_to_dict(plc.compile_hardware())
                result["hardware"] = hardware
                result["ok"] = result["ok"] and hardware["ok"]
            if normalized in {"software", "both"}:
                software = execution_result_to_dict(plc.compile_software())
                result["software"] = software
                result["ok"] = result["ok"] and software["ok"]
            return user_actions.attach(
                result, result.get("hardware"), result.get("software")
            )

    @staticmethod
    def _set_download_option(config: Any, method_name: str, value: bool) -> None:
        """设置一个下载安全选项。"""
        method = getattr(config, method_name, None)
        if not callable(method):
            if not value:
                # 旧版 Openness 没有部分新增安全选项；未请求该能力时跳过即可。
                return
            raise RuntimeError(f"当前 TIA 库不支持下载安全配置项：{method_name}")
        method(value=value)

    def _configure_download(
        self,
        plc: Any,
        download_mode: str,
        allow_cpu_stop: bool,
        allow_db_reinitialization: bool,
        allow_target_difference: bool,
        start_cpu_after_download: bool,
        plc_master_secret_password: str | None = None,
        module_read_password: str | None = None,
        module_write_password: str | None = None,
    ) -> None:
        """按授权参数配置 PLC 下载范围与安全选项。"""
        ts = self._library()
        enum = ts.Enums.GeneralDownloadOptions
        mode_names = {
            "software-changes": "SoftwareOnlyChanges",
            "software": "Software",
            "hardware": "Hardware",
            "hardware-software": "HardwareAndSoftware",
            "hardware-software-changes": "HardwareAndSoftwareOnlyChanges",
        }
        if download_mode not in mode_names:
            raise ValueError(
                f"无效 download_mode：{download_mode}；可选项：{list(mode_names)}"
            )
        native_mode = getattr(enum, mode_names[download_mode], None)
        if native_mode is None:
            raise RuntimeError(
                f"{self.connected_tia_version().label} 的 Openness 适配器不支持下载模式 "
                f"{download_mode!r}；请改用该版本提供的其他下载模式。"
            )
        config = plc.get_download_configuration()
        config.set_download_options(options=native_mode)
        config.set_check_before_download(value=True)
        safety_options = {
            "set_upgrade_target_device": False,
            "set_downgrade_target_device": False,
            "set_initialize_memory_selection": False,
            "set_reset_module_selection": False,
            "set_stop_modules_selection": allow_cpu_stop,
            "set_start_modules_selection": start_cpu_after_download,
            "set_data_block_reinitialization_selection": allow_db_reinitialization,
            "set_different_target_configuration_selection": allow_target_difference,
        }
        for method_name, value in safety_options.items():
            self._set_download_option(config, method_name, value)
        # S7-1200 G2 的“机密 PLC 组态数据/模块访问”密码：只有调用方显式提供时才设置，
        # 不猜测、不使用空密码覆盖。
        password_options = {
            "set_plc_master_secret_password": plc_master_secret_password,
            "set_module_read_access_password": module_read_password,
            "set_module_write_access_password": module_write_password,
        }
        for method_name, password in password_options.items():
            if password is None:
                continue
            method = getattr(config, method_name, None)
            if not callable(method):
                raise RuntimeError(
                    f"当前 Openness 适配器不支持 {method_name}；无法应用 PLC 访问密码。"
                )
            method(password=password)

    def download_to_plc(
        self,
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
        """确认目标和编译结果后下载到 PLC。"""
        with self._lock:
            if not confirm_download:
                raise ValueError("下载 PLC 必须设置 confirm_download=true。")
            is_simulation = "plcsim" in pc_interface_name.casefold()
            if not is_simulation and not allow_physical_plc:
                raise ValueError(
                    "当前接口不像 PLCSIM；实体 PLC 下载必须设置 allow_physical_plc=true。"
                )
            plc = self._require_plc(plc_name)
            self._assert_offline(plc)
            target_interface = self.normalize_online_target(target_interface)
            compile_result = None
            if compile_before_download:
                # 纯软件下载没有必要因为硬件组态错误而中止；硬件/软硬一起下载才编译 both。
                compile_scope = (
                    "software"
                    if download_mode in {"software", "software-changes"}
                    else "both"
                )
                compile_result = self.compile_plc(
                    compile_scope, str(plc.get_name())
                )
                if not compile_result["ok"]:
                    skipped = {
                        "ok": False,
                        "download_skipped": True,
                        "reason": "编译包含错误。",
                        "compile": compile_result,
                    }
                    return user_actions.attach(
                        skipped,
                        compile_result.get("hardware"),
                        compile_result.get("software"),
                    )
            self._configure_download(
                plc,
                download_mode,
                allow_cpu_stop,
                allow_db_reinitialization,
                allow_target_difference,
                start_cpu_after_download,
                plc_master_secret_password,
                module_read_password,
                module_write_password,
            )
            native_result = plc.download(
                pc_interface_type=pc_interface_type,
                pc_interface_name=pc_interface_name,
                target_interface=target_interface,
            )
            download_result = execution_result_to_dict(native_result)
            result = {
                "ok": download_result["ok"],
                "plc": str(plc.get_name()),
                "interface": {
                    "type": pc_interface_type,
                    "name": pc_interface_name,
                    "target": target_interface,
                    "simulation": is_simulation,
                },
                "download_mode": download_mode,
                "compile": compile_result,
                "download": download_result,
                "requested_start_after_download": start_cpu_after_download,
            }
            return user_actions.attach(
                result, download_result, compile_result
            )

    def save_project(self, confirm_save: bool = False) -> dict[str, Any]:
        """在显式确认后保存当前工程。"""
        with self._lock:
            if not confirm_save:
                raise ValueError("保存工程必须设置 confirm_save=true。")
            project = self._require_project()
            project.save()
            return {"ok": True, "project": _object_summary(project), "saved": True}

    def archive_project(
        self,
        target_directory: str,
        archive_name: str,
        overwrite: bool = False,
        confirm_archive: bool = False,
    ) -> dict[str, Any]:
        """在显式确认后归档当前工程。"""
        with self._lock:
            if not confirm_archive:
                raise ValueError("归档工程必须设置 confirm_archive=true。")
            raw_name = str(archive_name or "").strip()
            if not raw_name:
                raise ValueError("archive_name 必须是非空字符串。")
            # TIA 会自己追加 .zap21；调用方带扩展名时先去掉，避免 .zap21.zap21。
            for suffix in (".zap21", ".zap"):
                if raw_name.casefold().endswith(suffix):
                    raw_name = raw_name[: -len(suffix)].strip()
                    break
            if not raw_name:
                raise ValueError("archive_name 去掉扩展名后不能为空。")
            target = output_path(target_directory, "target_directory")
            target.mkdir(parents=True, exist_ok=True)
            result = self._require_project().archive(
                target_directory_path=str(target),
                archive_name=raw_name,
                delete_existing_archive=overwrite,
            )
            return {
                "ok": True,
                "archive": str(result),
                "archive_name": raw_name,
                "target": str(target),
            }

    def compare_to_online(self, plc_name: str | None = None) -> dict[str, Any]:
        """比较工程 PLC 与在线 PLC。

        注意：底层 compare_to_online() 的返回值表示「内容是否不同」
        （内容不同时为真），不能直接当作 equal_to_online 返回，否则语义取反，
        会让调用方把「尚未下载」误判为「已一致」。
        """
        with self._lock:
            plc = self._require_plc(plc_name)
            online_state = str(plc.get_online_state())
            if online_state.strip().casefold() != "online":
                return {
                    "ok": False,
                    "plc": str(plc.get_name()),
                    "online_state": online_state,
                    "error_code": "not_online",
                    "equal_to_online": None,
                    "content_different": None,
                    "differences": [],
                    "message": (
                        "PLC 当前不在线，无法进行离线/在线比较。"
                        "请先调用 go_online 建立连接，再重试。"
                    ),
                }
            raw = plc.compare_to_online()
            content_different = bool(raw)
            differences: list[str] = []
            if not isinstance(raw, bool):
                try:
                    differences = [str(item) for item in raw]
                except TypeError:
                    differences = []
            return {
                "ok": True,
                "plc": str(plc.get_name()),
                "equal_to_online": not content_different,
                "content_different": content_different,
                "differences": differences,
            }

    def backup_plc_software(
        self,
        target_directory: str,
        plc_name: str | None = None,
    ) -> dict[str, Any]:
        """导出 PLC 软件对象到调用方指定目录作为备份。"""
        with self._lock:
            if not target_directory.strip():
                raise ValueError("target_directory 必须是非空的备份目录路径。")
            plc = self._require_plc(plc_name)
            root = output_path(target_directory, "target_directory")
            root.mkdir(parents=True, exist_ok=True)
            sections: dict[str, Any] = {}
            failed_objects: list[dict[str, str]] = []
            collections = (
                ("program_blocks", plc.get_program_blocks()),
                ("plc_tag_tables", plc.get_plc_tag_tables()),
                ("udts", plc.get_user_data_types()),
                ("watch_tables", plc.get_watch_tables()),
            )
            for section, objects in collections:
                exported: list[str] = []
                failed: list[dict[str, str]] = []
                for obj in list(objects):
                    name = _safe_get_name(obj)
                    try:
                        result = self._export_objects(
                            [obj], root / section, "simaticml"
                        )
                    except Exception as exc:
                        failed.append(
                            {"name": name, "error": str(exc) or repr(exc)}
                        )
                        continue
                    exported.extend(result.get("exported") or [])
                failed_objects.extend(failed)
                sections[section] = {
                    "ok": not failed,
                    "exported": exported,
                    "failed": failed,
                }
            return {
                "ok": not failed_objects,
                "plc": str(plc.get_name()),
                "target_directory": str(root),
                "sections": sections,
                "failed_objects": failed_objects,
                "next_step": (
                    "部分对象处于不一致状态，Openness 拒绝导出；"
                    "先调用 compile_plc(scope=\"software\") 编译，再重新备份。"
                    if failed_objects
                    else "备份完成。"
                ),
            }

    @staticmethod
    def network_adapters() -> dict[str, Any]:
        """列出 Windows 网卡及其 IPv4 地址。"""
        script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Get-NetAdapter | Sort-Object Name | ForEach-Object {
    $ipv4 = (Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 `
        -ErrorAction SilentlyContinue | Select-Object -ExpandProperty IPAddress) -join ', '
    [PSCustomObject]@{
        Name = $_.Name
        InterfaceDescription = $_.InterfaceDescription
        Status = $_.Status
        LinkSpeed = $_.LinkSpeed
        IPv4 = $ipv4
    }
} | ConvertTo-Json -Compress
"""
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "读取 Windows 网卡失败。")
        raw = completed.stdout.strip().lstrip("\ufeff")
        values = json.loads(raw) if raw else []
        if isinstance(values, dict):
            values = [values]
        return {"ok": True, "adapters": values}
