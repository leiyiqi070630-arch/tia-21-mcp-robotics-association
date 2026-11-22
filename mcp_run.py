"""TIA Portal V21 MCP 服务启动入口（仅支持 V21）。"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import unicodedata

from mcp.server import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from api.mcp_api import registered_tool_names, register_tools, service
from core.console_runtime import ConsoleRuntime, ToolCallCounter, open_log_directory
from core.knowledge_base import knowledge_base_instruction, knowledge_base_log_line
from core.output_templates import output_template_instruction, template_log_line
from core.programming_policy import (
    PREFERRED_BLOCK_ENV,
    PREFERRED_LANGUAGE_ENV,
    programming_instruction,
    programming_policy,
)


# 版本号单一来源：MCPServer 元数据与启动横幅都引用它，避免两处版本不一致。
MCP_VERSION = "2.3.0"
PROGRAMMING_INSTRUCTION = programming_instruction()
OUTPUT_TEMPLATE_INSTRUCTION = output_template_instruction()
KNOWLEDGE_BASE_INSTRUCTION = knowledge_base_instruction()


class _SuppressAccessLog(logging.Filter):
    """隐藏每次 MCP 调用产生的 Uvicorn 访问日志。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return False


def _configure_console_logging() -> None:
    """保留启动与错误日志，只隐藏重复的 POST /mcp 200 OK。"""
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, _SuppressAccessLog) for item in access_logger.filters):
        access_logger.addFilter(_SuppressAccessLog())


mcp = MCPServer(
    name="tia-portal-v21-mcp",
    title="Siemens TIA Portal V21 MCP",
    description="通过 siemens_tia_scripting 控制 TIA Portal V21（仅支持 V21）。",
    version=MCP_VERSION,
    instructions=(
        "本项目仅支持 TIA Portal V21（Openness 21.0 / 21.1），不会连接 V15-V20 或其它版本。"
        "用户要求连接其它版本时，必须明确拒绝并说明本 MCP 的版本限制，不得尝试猜测或绕过。"
        "首次连接或 MCP EXE 更新、移动后，TIA Portal 会显示 Openness 防火墙授权窗口；"
        "这是 Siemens 的正常安全机制，不是版本错误。调用 connect_tia 前先提醒用户查看"
        "TIA 窗口；用户确认程序路径无误后，应人工选择“是，全部接受”(Yes to all)并确认 UAC。"
        "不得自动点击或绕过该安全授权。连接前先调用 check_openness_prerequisites；"
        "若当前用户不在 Siemens TIA Openness 组，必须明确告知添加用户组并注销重登的步骤。"
        "可调用 get_openness_access_guide 查看完整授权说明。"
        "任何工具返回 needs_user_action=true 时，必须停止自动重试，把 user_action.steps "
        "原样告诉用户并等待用户确认；若只返回 advisory=true（例如知识库未配置或不可读），"
        "告知即可，不阻塞本轮任务；不得猜测密码、不得绕过证书/授权、不得声称已自动完成。"
        "典型的人工场景：首次转在线需在 TIA 里信任 PLC 证书（Openness 无法点击）；"
        "S7-1200 G2 的机密 PLC 组态数据密码需在 TIA 中输入或随 download_to_plc 传入；"
        "PG/PC 接口名必须用网卡 InterfaceDescription，不能用 WLAN 别名。"
        "然后调用 connect_tia（version=auto、21.0 或 21.1），再读取组态或选择 PLC。"
        "不清楚本机情况时先调用 detect_tia_installations：一次返回已安装 V21、运行中"
        "实例、可连接版本与推荐动作；其它版本会出现在 blocked_installed 中，不可连接。"
        "没有运行实例时可用 launch_tia 自动启动 V21 并可选直接打开工程；同时安装"
        "21.0/21.1 时必须显式指定 version，且首次连接/EXE 变更后仍会弹出 Openness"
        "授权窗口，必须提醒用户人工确认。"
        "用户要求新建项目时，直接调用 create_tia_project（version 默认 auto 即 V21）；"
        "不要为其它版本创建工程。"
        "新增设备用 add_device_from_aml 或 add_device_from_template：桥接层没有 "
        "CreateWithItem，但 CAx(AML) 导入能真正建出设备与模块，并可在导入前改写 "
        "PROFINET IP 与子网掩码；没有模板时先用 export_project_aml 从含目标机型的工程"
        "导出，或用 list_device_templates 查看现有模板。对已存在设备直接改写 IP 不被"
        "Openness 支持（rc=20），必须如实告知只能随 AML 导入写入。"
        "修改工程前应转离线；可用 get_tia_compatibility 查看当前 V21 能力。"
        "调用 go_online 时，若用户尚未指定连接方式，应让用户选择仿真或物理接口；"
        "物理接口的网卡名必须由用户填写。若工具返回 needs_input，先询问用户再重试。"
        "删除、写入、导入、保存和下载工具需要显式确认参数；不要替用户猜测"
        "网卡、目标接口或实体 PLC 授权。V21 的 target_interface 填 PLC 目标接口名"
        "（默认 '1 X1'），不能填写 IP 地址。"
        "硬件参数先用 get_hardware_inventory 读取目标对象真实属性名，再用"
        "set_hardware_properties 或 configure_s71200_hsc 写入；必须离线、显式确认并检查"
        "写后回读和硬件编译结果。不同 CPU/固件的 HSC 属性名不得硬编码。"
        "PLC 防护与安全先用 get_plc_security_guide / get_plc_security_settings 读取实际可达属性；"
        "只写白名单内的 access_control、put_get_communication、web_server、syslog、"
        "security_events 等属性，写入后必须编译硬件并下载才生效。机密组态密码、访问等级、"
        "用户管理、证书管理器没有 Openness API，必须按 pages[].manual_steps 让用户人工配置，"
        "不得猜测属性名或声称已自动完成。"
        "可达性判断：get_hardware_inventory 的 summary 与 find_hardware_property 会说明"
        "属性是否真的可达；若返回 hsc_properties_reachable=false 或 filter_matched_nothing，"
        "说明该参数不在桥接层暴露的设备/PLC/模块属性里（可达性边界，不是属性名映射问题），"
        "应如实告知用户在 TIA Portal 中人工组态，不要反复猜属性名。"
        "工艺对象先用 get_technology_object_details 读取实际属性和一致性；创建或覆盖对象"
        "采用 V21、兼容 CPU 的 SimaticML 模板，先 export_technology_objects 备份，"
        "再离线调用 import_technology_objects。属性修改使用 set_technology_object_properties，"
        "并检查回读、对象级编译和回滚结果；删除工艺对象必须先指定外部备份目录。"
        "桥接层没有通用工艺对象 Create API，不能猜测内部 XML 或强制跨版本导入。"
        + PROGRAMMING_INSTRUCTION
        + OUTPUT_TEMPLATE_INSTRUCTION
        + KNOWLEDGE_BASE_INSTRUCTION
        + "V21 原生支持紧凑 LAD 自动生成 SIMATIC SD。"
        "LAD 规格可使用 move、compare、ctrl_hsc 和通用 instruction 指令盒。"
        "LAD 操作数必须是 #局部变量或 PLC 符号（\"符号名\"），不支持绝对地址 %M0.0；"
        "需要绝对地址时先在 PLC 变量表中定义符号再引用。"
        "导入后检查编译诊断，不要把长篇源文档重复返回给模型。"
        "需要给程序块建目录时用 create_program_block_group(group_path=\"A/B\", "
        "confirm_create=true)，必须离线；创建后调用 save_project 才会落盘。"
        "所有文件路径使用绝对路径；持久备份和导出必须保存在服务安装目录之外。"
    ),
)
register_tools(mcp)


# 工具数量直接取注册表长度；新增工具时不需要再同步任何常量。
TOOL_COUNT = len(registered_tool_names())
BANNER_INNER_WIDTH = 62
FOCUS_TEXT = "机器人协会，欢迎交流工业自动化与 AI 技术"

# 桌面控制台读取的运行期统计：会话日志、今日/本次启动访问次数、历史曲线。
runtime = ConsoleRuntime()


@mcp.custom_route("/console/ping", methods=["GET"])
async def _console_ping(request: Request) -> JSONResponse:
    """轻量探活：桌面控制台用它判断端口上是否已经是本 MCP 服务。"""
    return JSONResponse({"ok": True, "service": "tia-portal-v21-mcp", "version": MCP_VERSION})


@mcp.custom_route("/console/stats", methods=["GET"])
async def _console_stats(request: Request) -> JSONResponse:
    """统计快照：``?days=7`` / ``?days=30`` 控制历史曲线长度。"""
    try:
        days = int(request.query_params.get("days", "30"))
    except (TypeError, ValueError):
        days = 30
    return JSONResponse(runtime.snapshot(days))


@mcp.custom_route("/console/open-log-dir", methods=["GET", "POST"])
async def _console_open_log_dir(request: Request) -> JSONResponse:
    """在服务进程里打开日志目录，供桌面控制台「打开日志目录」调用。"""
    return JSONResponse(open_log_directory(runtime.log_dir))


@mcp.custom_route("/console/health", methods=["GET"])
async def _console_health(request: Request) -> JSONResponse:
    """进程级健康检查：确认服务线程仍在响应，且 Openness 组件可用。"""
    from core.tia_service import openness_assembly_missing

    missing = openness_assembly_missing()
    return JSONResponse(
        {
            "ok": True,
            "pid": os.getpid(),
            "openness_ready": not missing,
            "openness_missing": missing,
        }
    )


@mcp.custom_route("/console/environment", methods=["GET"])
async def _console_environment(request: Request) -> JSONResponse:
    """环境快照：博途是否已打开、Openness 用户组、程序集与当前连接状态。

    控制台主页每 5 秒拉一次。这里只做 Windows 进程枚举与 ``whoami`` 查询，
    不碰 Openness 的 TIA 执行线程，所以不会与正在进行的编译/写入抢锁。
    """
    from core.tia_service import environment_report

    report = environment_report()
    connection: dict[str, object] = {"connected": False}
    try:
        connection = {
            "connected": service.portal is not None,
            "version": service.connected_version,
            "project": getattr(service.project, "name", None),
            "plc": service.selected_plc_name,
            "pid": os.getpid(),
        }
    except Exception:  # noqa: BLE001 - 环境探测不能影响服务
        pass
    report["service"] = connection
    return JSONResponse(report)


@mcp.custom_route("/console/shutdown", methods=["POST", "GET"])
async def _console_shutdown(request: Request) -> JSONResponse:
    """优雅关停：先 detach TIA，再结束进程。

    控制台停止服务时优先走这里，避免直接 ``taskkill`` 打断 TIA 侧的
    Openness 会话（那会让下一次 attach 抛 TargetInvocationException）。
    """
    detached = False
    try:
        detached = bool(service.disconnect().get("ok"))
    except Exception:  # noqa: BLE001 - 关停路径要尽量走完
        detached = False
    try:
        runtime.stop()
    except Exception:  # noqa: BLE001
        pass

    def _exit_soon() -> None:
        time.sleep(0.4)
        os._exit(0)

    threading.Thread(target=_exit_soon, name="console-shutdown", daemon=True).start()
    return JSONResponse(
        {
            "ok": True,
            "detached": detached,
            "message": "MCP 服务正在关停（已先断开 TIA 连接）。",
        }
    )


mcp.middleware.append(ToolCallCounter(runtime))


def _display_width(text: str) -> int:
    """按终端显示宽度计算字符串长度。

    中文/全角（W、F）按 2 列，其余按 1 列。注意两类"东亚模糊宽度(A)"字符在中文
    控制台里表现相反，不能统一处理：

    * 制表符 ``─│┌┐└┘`` 是单列，若按 2 列算会把整条边框算长一倍；
    * ``·`` ``•`` 这类符号在新宋体等中文字体里是双列，按 1 列算会让该行短 1 列，
      右边框与其它行错位。

    因此框内文本一律避免出现模糊宽度符号（用全角标点或纯中文停顿代替），
    宽度只按 W/F 计算。
    """
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _banner_row(text: str = "", width: int = BANNER_INNER_WIDTH) -> str:
    """生成一行框内文本，并按显示宽度补齐到指定宽度。"""
    padding = max(width - 4 - _display_width(text), 0)
    return "│  " + text + " " * padding + "  │"


def _banner_rule(left: str, right: str, width: int = BANNER_INNER_WIDTH) -> str:
    """生成分隔线；总宽度与对应框内行保持一致。"""
    return left + "─" * width + right


def _banner_box(lines: list[str], width: int = BANNER_INNER_WIDTH) -> list[str]:
    """把若干行文本包成一个闭合方框。"""
    return (
        [_banner_rule("┌", "┐", width)]
        + [_banner_row(line, width) for line in lines]
        + [_banner_rule("└", "┘", width)]
    )


def startup_banner() -> list[str]:
    """生成启动横幅：信息框 + 关注框 + 授权提示，按显示宽度对齐。"""
    info_lines = [
        f"版本   v{MCP_VERSION}",
        "接口   http://127.0.0.1:8000/mcp",
        f"工具   {TOOL_COUNT} 个",
        "支持   TIA Portal V21",
        "限制   其它版本一律拒绝连接",
    ]
    return _banner_box(info_lines) + [""] + _banner_box([FOCUS_TEXT]) + [""]


def _port_available(host: str, port: int) -> bool:
    """启动前探测端口是否可绑定，避免 uvicorn 直接抛 traceback。

    Windows 上不能用 SO_REUSEADDR 做探测：它允许绑定已被监听的端口，会让
    第二个实例误以为端口空闲。非 Windows 才启用 REUSEADDR（便于快速重启）。
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name != "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


def _wait_for_key() -> None:
    """控制台交互时等待任意键，避免双击 EXE 时窗口闪退。

    非交互环境（服务/管道/CI）直接返回，不阻塞启动失败后的退出流程。
    """
    stdin = sys.stdin
    if stdin is None:
        return
    try:
        if not stdin.isatty():
            return
    except Exception:
        return
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.getwch()
        else:
            stdin.read(1)
    except (EOFError, KeyboardInterrupt, OSError):
        pass


def _report_port_conflict(host: str, port: int) -> None:
    """端口被占用时给出可执行的提醒，并等待用户按键，避免窗口闪退。"""
    print("")
    print("=" * 64)
    print(f"  端口 {host}:{port} 已被占用。")
    print("  请检查是否重复启动本 MCP 服务。")
    print("  当前 MCP 已经停止运行。")
    print("  请按任意键退出，或直接关闭本服务。")
    print("=" * 64)
    print("")
    print(f"  如果 MCP 已经在运行，可直接连接 http://{host}:{port}/mcp")
    print(f"  查看占用者：  netstat -ano | findstr :{port}")
    print("  结束占用者：  taskkill /PID <最后一列的PID> /F")
    print("")
    try:
        sys.stdout.flush()
    except Exception:
        pass
    _wait_for_key()


def _openness_prerequisite() -> dict:
    """读取当前用户的 Openness 组成员资格；失败时返回可展示的 blocked 结果。"""
    try:
        return service.openness_prerequisites()
    except Exception as error:  # noqa: BLE001 - 启动检查不能因诊断异常而崩溃
        return {
            "ok": False,
            "group_membership": "unknown",
            "current_user": "",
            "message": f"检查 Siemens TIA Openness 用户组时出错：{error}",
            "action_steps": [],
        }


def _openness_check_lines(status: dict) -> list[str]:
    """把 Openness 检查结果压缩成方格中的两行。"""
    if status.get("ok") and status.get("group_membership") == "member":
        user = status.get("current_user") or "当前用户"
        return [
            f"Openness 用户组：已加入（用户 {user}）",
            "组名   Siemens TIA Openness",
        ]
    if status.get("group_membership") == "missing":
        return ["Openness 用户组：未加入", "需要加入 Siemens TIA Openness"]
    return ["Openness 用户组：无法确认", "需要加入 Siemens TIA Openness"]


def _print_startup_checks(port_ok: bool, openness: dict) -> None:
    """把端口与 Openness 两项检测结果放进同一个方格。"""
    port_line = "端口   127.0.0.1:8000 " + ("检测正常" if port_ok else "已被占用")
    for line in _banner_box([port_line] + _openness_check_lines(openness)):
        print(line)
    print("")
    try:
        sys.stdout.flush()
    except Exception:
        pass


def _report_openness_blocked(status: dict) -> None:
    """未加入/无法确认 Openness 用户组时给出处理步骤，并等待按键。"""
    membership = status.get("group_membership")
    user = status.get("current_user") or "未知"
    print("")
    if membership == "missing":
        print("  未加入「Siemens TIA Openness」用户组，MCP 服务未启动。")
    else:
        print("  无法确认「Siemens TIA Openness」用户组成员资格，MCP 服务未启动。")
    print(f"  当前用户：{user}")
    message = status.get("message")
    if message:
        print(f"  说明：{message}")
    steps = status.get("action_steps") or []
    if steps:
        print("  处理步骤：")
        for index, step in enumerate(steps, 1):
            print(f"    {index}. {step}")
    print("  请按任意键退出，或直接关闭本服务。")
    print("")
    try:
        sys.stdout.flush()
    except Exception:
        pass
    _wait_for_key()


def _log_programming_preference() -> None:
    """把本次启动生效的编程偏好写进会话日志，便于核对控制台开关是否真的传到服务。"""
    policy = programming_policy()
    logging.getLogger("tia-mcp.console").info(
        "[偏好] 默认语言 %s | 默认块架构 %s | %s=%s %s=%s",
        policy["language_label"],
        "Main(OB1)" if policy["preferences"]["preferred_block"] == "MAIN" else "FB/FC 拆分",
        PREFERRED_LANGUAGE_ENV,
        policy["preferences"]["preferred_language"],
        PREFERRED_BLOCK_ENV,
        policy["preferences"]["preferred_block"],
    )


def _log_output_template() -> None:
    """把控制台配置的输出模板写进会话日志，便于核对是否传到服务。"""
    logging.getLogger("tia-mcp.console").info("%s", template_log_line())


def _log_knowledge_base() -> None:
    """把控制台配置的知识库根目录状态写进会话日志。"""
    logging.getLogger("tia-mcp.console").info("%s", knowledge_base_log_line())


def _emit_service_info() -> None:
    """``--info`` 输出机器可读信息后退出，供桌面控制台读取版本与工具数量。"""
    payload = {"ok": True, "version": MCP_VERSION, "tool_count": TOOL_COUNT}
    print(json.dumps(payload, ensure_ascii=False))
    try:
        sys.stdout.flush()
    except Exception:
        pass


def main() -> None:
    """启动 MCP 服务；端口与 Openness 用户组两项检查都通过后才监听。"""
    if "--info" in sys.argv[1:]:
        _emit_service_info()
        return
    try:
        _configure_console_logging()
        try:
            for line in startup_banner():
                print(line)
            sys.stdout.flush()
        except Exception:
            # 极端编码环境下退化为单行，保证服务照常启动。
            print(f"TIA Portal MCP v{MCP_VERSION} | http://127.0.0.1:8000/mcp")
        port_ok = _port_available("127.0.0.1", 8000)
        openness = _openness_prerequisite()
        _print_startup_checks(port_ok, openness)
        if not port_ok:
            _report_port_conflict("127.0.0.1", 8000)
            sys.exit(2)
        if not openness.get("ok"):
            _report_openness_blocked(openness)
            sys.exit(2)
        print("  启动前检查全部通过，正在启动 MCP 服务……")
        print("")
        sys.stdout.flush()
        runtime.start(version=MCP_VERSION, tool_count=TOOL_COUNT)
        _log_programming_preference()
        _log_output_template()
        _log_knowledge_base()
        try:
            mcp.run(
                transport="streamable-http",
                host="127.0.0.1",
                port=8000,
                streamable_http_path="/mcp",
            )
        finally:
            runtime.stop()
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()
