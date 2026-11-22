"""桌面控制台「输出模板」。

用户在控制台「推理设置 → 输出模板」里选择本地 Markdown 文件，控制台把路径通过
环境变量传给 MCP 服务。工具调用时才读取文件内容，所以改模板正文不需要重启服务，
只有更换模板路径才需要（路径来自启动时的环境变量）。

模板只影响 AI 的最终答复格式，不参与任何 TIA 操作。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any


TEMPLATE_CONNECT_ENV = "TIA_MCP_CONNECT_TEMPLATE"

DEFAULT_TEMPLATE_KIND = "connect"

# kind -> 模板定义。新增用途时在这里登记，工具与提示会自动支持。
TEMPLATE_KINDS: dict[str, dict[str, str]] = {
    "connect": {
        "env": TEMPLATE_CONNECT_ENV,
        "title": "连接博途输出模板",
        "usage": (
            "调用 get_output_template(kind=\"connect\") 读取模板后，按模板的章节、表格与字段"
            "输出连接结果；示例值只是占位，必须替换为本次连接的真实值，"
            "模板没有要求的字段不要编造。"
        ),
    },
}

# 单个模板的读取上限：模板是给人看的说明文件，不需要支持超大文件。
MAX_TEMPLATE_BYTES = 512 * 1024
MAX_TEMPLATE_CHARS = 20000


def _normalize_kind(kind: str | None) -> str | None:
    normalized = str(kind or "").strip().lower().replace("-", "_")
    return normalized if normalized in TEMPLATE_KINDS else None


def template_kinds() -> list[str]:
    """返回当前支持的模板类型。"""
    return sorted(TEMPLATE_KINDS)


def _template_path(kind: str) -> Path | None:
    raw = str(os.environ.get(TEMPLATE_KINDS[kind]["env"]) or "").strip().strip('"')
    return Path(raw).expanduser() if raw else None


def template_status(kind: str = DEFAULT_TEMPLATE_KIND) -> dict[str, Any]:
    """返回模板配置与文件状态；不读取正文，供控制台/日志/提示复用。"""
    normalized = _normalize_kind(kind)
    if normalized is None:
        raise ValueError(f"未知的模板类型：{kind}")

    definition = TEMPLATE_KINDS[normalized]
    path = _template_path(normalized)
    status: dict[str, Any] = {
        "kind": normalized,
        "title": definition["title"],
        "env": definition["env"],
        "configured": path is not None,
        "path": str(path) if path else "",
        "file_name": path.name if path else "",
        "exists": False,
        "size_bytes": None,
        "modified_at": None,
        "error": "",
    }
    if path is None:
        return status

    try:
        stat = path.stat()
    except OSError as error:
        status["error"] = f"模板文件不存在或无法访问：{error}"
        return status
    if not path.is_file():
        status["error"] = "配置的路径不是文件。"
        return status

    status.update(
        exists=True,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime)
        .astimezone()
        .isoformat(timespec="seconds"),
    )
    return status


def _decode_template(raw: bytes) -> tuple[str, str]:
    """模板以 UTF-8 为主；兼容用户从旧工程导出的 GB18030 文本。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def read_output_template(kind: str = DEFAULT_TEMPLATE_KIND) -> dict[str, Any]:
    """读取模板正文并附上状态；供 ``get_output_template`` 工具直接返回。"""
    normalized = _normalize_kind(kind)
    if normalized is None:
        return {
            "ok": False,
            "error": f"未知的模板类型：{kind}",
            "available_kinds": template_kinds(),
        }

    title = TEMPLATE_KINDS[normalized]["title"]
    status = template_status(normalized)
    if not status["configured"]:
        return {
            "ok": True,
            "configured": False,
            "readable": False,
            "kind": normalized,
            "title": title,
            "message": (
                f"用户尚未在桌面控制台「推理设置 → 输出模板」配置「{title}」；"
                "本次按默认格式简洁汇报即可。"
            ),
        }

    if not status["exists"]:
        return {
            "ok": True,
            "configured": True,
            "readable": False,
            "kind": normalized,
            "title": title,
            "path": status["path"],
            "file_name": status["file_name"],
            "error": status["error"],
            "needs_user_action": True,
            "user_action": {
                "code": "select_output_template",
                "title": "需要重新选择输出模板",
                "why_mcp_cannot_do_it": "模板路径由用户在桌面控制台中选择，MCP 只负责读取。",
                "steps": [
                    "打开控制台「推理设置 → 输出模板」。",
                    f"点击「选择文件…」重新选择「{title}」的 Markdown 文件。",
                ],
                "retry": "用户重新选择后，再次调用 get_output_template(kind=\"connect\")。",
            },
            "message": "模板文件当前不可读；先告知用户重新选择，连接结果按默认格式汇报。",
        }

    try:
        raw = Path(status["path"]).read_bytes()
    except OSError as error:  # 状态查询与读取之间文件可能被删除
        return {
            "ok": True,
            "configured": True,
            "readable": False,
            "kind": normalized,
            "title": title,
            "path": status["path"],
            "file_name": status["file_name"],
            "error": f"读取模板失败：{error}",
            "message": "模板文件读取失败；按默认格式汇报并提醒用户重新选择模板。",
        }

    truncated = False
    if raw is not None and len(raw) > MAX_TEMPLATE_BYTES:
        raw = raw[:MAX_TEMPLATE_BYTES]
        truncated = True
    text, encoding = _decode_template(raw or b"")
    if len(text) > MAX_TEMPLATE_CHARS:
        text = text[:MAX_TEMPLATE_CHARS]
        truncated = True

    return {
        "ok": True,
        "configured": True,
        "readable": True,
        "kind": normalized,
        "title": title,
        "path": status["path"],
        "file_name": status["file_name"],
        "size_bytes": status["size_bytes"],
        "modified_at": status["modified_at"],
        "encoding": encoding,
        "truncated": truncated,
        "usage": TEMPLATE_KINDS[normalized]["usage"],
        "content": text,
    }


def output_template_hint(kind: str = DEFAULT_TEMPLATE_KIND) -> str:
    """给工具描述用的一句话模板提示（模型拉工具列表即可看到）。"""
    normalized = _normalize_kind(kind) or DEFAULT_TEMPLATE_KIND
    title = TEMPLATE_KINDS[normalized]["title"]
    status = template_status(normalized)

    if not status["configured"]:
        return (
            f"【输出模板】桌面控制台未配置「{title}」；连接完成后按默认格式简洁汇报，"
            "并提示用户可在「推理设置 → 输出模板」选择 Markdown 模板。"
        )
    if not status["exists"]:
        return (
            f"【输出模板】「{title}」已配置但文件不可读；先提醒用户在控制台重新选择模板文件，"
            "连接结果按默认格式汇报。"
        )
    return (
        f"【输出模板】已配置「{title}」（{status['file_name']}）：完成 connect_tia、"
        f"get_tia_status 后先调用 get_output_template(kind=\"{normalized}\")，"
        "严格按模板章节、表格列与字段输出；示例值必须替换为本次连接的真实值。"
    )


def output_template_instruction(kind: str = DEFAULT_TEMPLATE_KIND) -> str:
    """生成注入 MCP Server instructions 的动态模板说明。"""
    normalized = _normalize_kind(kind) or DEFAULT_TEMPLATE_KIND
    title = TEMPLATE_KINDS[normalized]["title"]
    status = template_status(normalized)

    if status["configured"] and status["exists"]:
        return (
            f"输出模板：桌面控制台已为「{title}」配置模板文件（{status['file_name']}）。"
            "完成 connect_tia 与 get_tia_status 后，必须先调用 "
            f"get_output_template(kind=\"{normalized}\")，再严格按返回 content 的章节标题、"
            "表格列与字段输出最终答复：只把示例值替换为本次连接的真实值（服务版本、TIA 版本、"
            "实际 PID、工程名与工程路径、PLC 名称/型号/订货号/固件/IP 等），"
            "不得增删或改名模板要求的章节与字段，不得照抄示例值，也不得编造模板未要求的字段。"
            "模板文件不可读时如实说明，并按默认格式汇报。"
        )
    if status["configured"]:
        return (
            f"输出模板：桌面控制台已配置「{title}」但模板文件不可读；"
            "先提醒用户在控制台重新选择模板文件，连接结果按默认格式汇报。"
        )
    return (
        "输出模板：桌面控制台尚未配置「连接博途输出模板」。完成连接后按默认格式简洁汇报"
        "（服务版本、TIA 版本、实际 PID、工程名、PLC 名），并提示用户可在控制台"
        "「推理设置 → 输出模板」选择 Markdown 模板。"
    )


def template_log_line(kind: str = DEFAULT_TEMPLATE_KIND) -> str:
    """启动日志用的一行模板状态，便于核对控制台配置是否真的传到服务。"""
    status = template_status(kind)
    title = status["title"]
    if not status["configured"]:
        return f"[模板] {title}：未配置"
    if not status["exists"]:
        return f"[模板] {title}：已配置但文件不可用 -> {status['path']}"
    return f"[模板] {title}：{status['path']}（{status['size_bytes']} 字节）"
