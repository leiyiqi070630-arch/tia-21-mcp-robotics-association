"""用户在桌面控制台选择的本地知识库。

知识库路径由 ``TIA_MCP_KNOWLEDGE_BASE_ROOT`` 注入。MCP 只暴露受根目录约束的
只读列表、检索和分段读取能力；不会写入知识库，也不会跟随符号链接逃逸到根目录外。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


KNOWLEDGE_BASE_ENV = "TIA_MCP_KNOWLEDGE_BASE_ROOT"

SUPPORTED_EXTENSIONS = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".csv",
        ".tsv",
        ".xml",
        ".ini",
        ".cfg",
        ".scl",
        ".st",
        ".awl",
        ".udt",
        ".db",
    }
)

MAX_LIST_RESULTS = 500
MAX_SCANNED_FILES = 2000
MAX_SEARCH_FILES = 500
MAX_SEARCH_RESULTS = 50
MAX_READ_CHARS = 30000
MAX_FILE_BYTES = 2 * 1024 * 1024
SNIPPET_CHARS = 360


def _raw_root() -> str:
    return str(os.environ.get(KNOWLEDGE_BASE_ENV) or "").strip().strip('"')


def knowledge_base_status() -> dict[str, Any]:
    """返回知识库配置与目录状态，不枚举文件。"""
    raw = _raw_root()
    status: dict[str, Any] = {
        "configured": bool(raw),
        "env": KNOWLEDGE_BASE_ENV,
        "root": raw,
        "exists": False,
        "readable": False,
        "error": "",
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
    }
    if not raw:
        return status
    try:
        root = Path(raw).expanduser().resolve(strict=True)
    except OSError as error:
        status["error"] = f"知识库目录不存在或无法访问：{error}"
        return status
    status["root"] = str(root)
    if not root.is_dir():
        status["error"] = "配置的知识库路径不是目录。"
        return status
    status["exists"] = True
    try:
        with os.scandir(root) as entries:
            next(entries, None)
        status["readable"] = True
    except OSError as error:
        status["readable"] = False
        status["error"] = f"知识库目录没有读取权限：{error}"
    return status


def knowledge_base_unavailable(status: dict[str, Any]) -> dict[str, Any]:
    configured = bool(status.get("configured"))
    return {
        "ok": True,
        "configured": configured,
        "readable": False,
        "root": status.get("root", ""),
        "error": status.get("error", ""),
        "advisory": True,
        "needs_user_action": False,
        "user_action": {
            "code": "select_knowledge_base",
            "title": "需要选择知识库目录" if not configured else "需要重新选择知识库目录",
            "why_mcp_cannot_do_it": "知识库根目录必须由用户在桌面控制台中选择。",
            "steps": [
                "打开控制台「推理设置 → 知识库」。",
                "点击「选择目录…」，选择当前用户自己的知识库根目录。",
            ],
            "retry": "保存后重新调用 get_knowledge_base_status。",
        },
        "message": (
            "尚未配置知识库；本次按通用规范继续。"
            if not configured
            else "知识库目录当前不可读；请让用户重新选择，本次按通用规范继续。"
        ),
    }


def _root_or_none() -> tuple[Path | None, dict[str, Any]]:
    status = knowledge_base_status()
    if not status["configured"] or not status["readable"]:
        return None, status
    return Path(status["root"]), status


def _safe_path(root: Path, relative_path: str, *, require_file: bool = False) -> Path:
    value = str(relative_path or "").strip().replace("\\", "/")
    relative = Path(value or ".")
    if relative.is_absolute():
        raise ValueError("只能使用知识库根目录内的相对路径。")
    try:
        candidate = (root / relative).resolve(strict=True)
    except OSError as error:
        raise ValueError(f"知识库路径不存在或无法访问：{relative_path}") from error
    if candidate != root and root not in candidate.parents:
        raise ValueError("拒绝访问知识库根目录之外的路径。")
    if require_file and not candidate.is_file():
        raise ValueError("指定路径不是知识库文件。")
    return candidate


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_supported(path: Path) -> bool:
    return path.suffix.casefold() in SUPPORTED_EXTENSIONS


def _iter_documents(
    root: Path,
    directory: Path,
    *,
    recursive: bool,
    scan_state: dict[str, Any] | None = None,
) -> Iterable[Path]:
    iterator = directory.rglob("*") if recursive else directory.iterdir()
    scanned = 0
    for candidate in iterator:
        scanned += 1
        if scanned > MAX_SCANNED_FILES:
            if scan_state is not None:
                scan_state["limited"] = True
            break
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved != root and root not in resolved.parents:
            continue
        if resolved.is_file() and _is_supported(resolved):
            yield resolved
    if scan_state is not None:
        scan_state["scanned"] = min(scanned, MAX_SCANNED_FILES)


def _decode(raw: bytes) -> tuple[str, str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def _read_text(path: Path) -> tuple[str, str]:
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(
            f"文件超过单文件读取上限 {MAX_FILE_BYTES // 1024 // 1024} MB，请拆分后再使用。"
        )
    return _decode(path.read_bytes())


def _original_offset(text: str, folded_offset: int) -> int:
    """把 casefold 后的字符位置换算回原文位置。

    ``str.casefold`` 可能把一个字符展开成多个字符（例如 ``ß``），直接拿
    casefold 位置去切片会偏移；这里按每个原字符的 casefold 长度累计定位。
    """
    if folded_offset <= 0:
        return 0
    cursor = 0
    for index, character in enumerate(text):
        cursor += len(character.casefold())
        if cursor > folded_offset:
            return index
    return max(0, len(text) - 1)


def list_knowledge_documents(
    relative_directory: str = "",
    recursive: bool = True,
    limit: int = 200,
) -> dict[str, Any]:
    """列出知识库中的可读文本资料，返回相对路径与截断状态。"""
    root, status = _root_or_none()
    if root is None:
        return knowledge_base_unavailable(status)
    try:
        directory = _safe_path(root, relative_directory)
        if not directory.is_dir():
            raise ValueError("relative_directory 不是目录。")
        normalized_limit = max(1, min(int(limit), MAX_LIST_RESULTS))
        scan_state: dict[str, Any] = {"limited": False, "scanned": 0}
        documents = []
        skipped = 0
        for file in _iter_documents(
            root,
            directory,
            recursive=bool(recursive),
            scan_state=scan_state,
        ):
            try:
                stat = file.stat()
            except OSError:
                skipped += 1
                continue
            documents.append(
                {
                    "relative_path": _relative(root, file),
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime)
                    .astimezone()
                    .isoformat(timespec="seconds"),
                }
            )
        documents.sort(key=lambda item: item["relative_path"].casefold())
        total_documents = len(documents)
        documents = documents[:normalized_limit]
        return {
            "ok": True,
            "configured": True,
            "readable": True,
            "root": str(root),
            "relative_directory": _relative(root, directory) if directory != root else "",
            "recursive": bool(recursive),
            "documents": documents,
            "count": len(documents),
            "limit": normalized_limit,
            "total_documents": total_documents,
            "truncated": total_documents > len(documents),
            "scanned_entries": int(scan_state.get("scanned", 0)),
            "scan_limited": bool(scan_state.get("limited")),
            "skipped_files": skipped,
        }
    except (OSError, ValueError) as error:
        return {"ok": False, "error": str(error), "root": str(root)}


def read_knowledge_document(
    relative_path: str,
    offset: int = 0,
    limit: int = 20000,
) -> dict[str, Any]:
    """按相对路径分段读取一份知识资料。"""
    root, status = _root_or_none()
    if root is None:
        return knowledge_base_unavailable(status)
    try:
        file = _safe_path(root, relative_path, require_file=True)
        if not _is_supported(file):
            raise ValueError(
                f"不支持的知识文件类型：{file.suffix or '(无扩展名)'}；"
                f"支持 {', '.join(sorted(SUPPORTED_EXTENSIONS))}。"
            )
        text, encoding = _read_text(file)
        start = max(0, int(offset))
        size = max(1, min(int(limit), MAX_READ_CHARS))
        end = min(len(text), start + size)
        return {
            "ok": True,
            "configured": True,
            "readable": True,
            "root": str(root),
            "relative_path": _relative(root, file),
            "encoding": encoding,
            "offset": start,
            "next_offset": end if end < len(text) else None,
            "total_chars": len(text),
            "truncated": end < len(text),
            "content": text[start:end],
        }
    except (OSError, ValueError) as error:
        return {"ok": False, "error": str(error), "root": str(root)}


def search_knowledge_base(
    query: str,
    relative_directory: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """按关键词检索知识库；空格分隔的多个词按“全部命中优先”排序。"""
    root, status = _root_or_none()
    if root is None:
        return knowledge_base_unavailable(status)
    terms = [term for term in str(query or "").casefold().split() if term]
    if not terms:
        return {"ok": False, "error": "query 不能为空。", "root": str(root)}
    try:
        directory = _safe_path(root, relative_directory)
        if not directory.is_dir():
            raise ValueError("relative_directory 不是目录。")
        normalized_limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
        matches = []
        scanned = 0
        skipped = 0
        scan_limited = False
        scan_state: dict[str, Any] = {"limited": False, "scanned": 0}
        for file in _iter_documents(
            root,
            directory,
            recursive=True,
            scan_state=scan_state,
        ):
            if scanned >= MAX_SEARCH_FILES:
                scan_limited = True
                break
            scanned += 1
            try:
                text, _encoding = _read_text(file)
            except (OSError, ValueError):
                skipped += 1
                continue
            folded = text.casefold()
            counts = [folded.count(term) for term in terms]
            matched_terms = [term for term, count in zip(terms, counts) if count > 0]
            if not matched_terms:
                continue
            positions = [folded.find(term) for term in matched_terms]
            folded_index = min(position for position in positions if position >= 0)
            first_index = _original_offset(text, folded_index)
            start = max(0, first_index - SNIPPET_CHARS // 3)
            end = min(len(text), start + SNIPPET_CHARS)
            snippet = " ".join(text[start:end].split())
            matches.append(
                {
                    "relative_path": _relative(root, file),
                    "match_count": sum(counts),
                    "matched_terms": matched_terms,
                    "all_terms_matched": len(matched_terms) == len(terms),
                    "first_match_offset": first_index,
                    "snippet": snippet,
                }
            )
        matches.sort(
            key=lambda item: (
                not item["all_terms_matched"],
                -item["match_count"],
                item["relative_path"].casefold(),
            )
        )
        total_matches = len(matches)
        matches = matches[:normalized_limit]
        scan_limited = scan_limited or bool(scan_state.get("limited"))
        return {
            "ok": True,
            "configured": True,
            "readable": True,
            "root": str(root),
            "query": query,
            "query_terms": terms,
            "matches": matches,
            "count": len(matches),
            "total_matches": total_matches,
            "truncated": total_matches > len(matches),
            "scanned_files": scanned,
            "skipped_files": skipped,
            "scan_limited": scan_limited,
        }
    except (OSError, ValueError) as error:
        return {"ok": False, "error": str(error), "root": str(root)}


def knowledge_base_hint() -> str:
    """给编程工具描述使用的知识库提示。"""
    status = knowledge_base_status()
    if status["configured"] and status["readable"]:
        return (
            "【知识库】控制台已配置本地知识库；编程前先调用 get_knowledge_base_status，"
            "再用 search_knowledge_base 检索任务相关资料，并只读取命中文档。"
        )
    if status["configured"]:
        return (
            "【知识库】控制台配置的知识库目录不可读；提醒用户重新选择即可，"
            "本次按通用规范继续（advisory=true，不阻塞）。"
        )
    return (
        "【知识库】控制台未配置知识库；本次按通用规范继续（advisory=true，不阻塞），"
        "不得猜测本地资料内容。"
    )


def knowledge_base_instruction() -> str:
    """生成注入 MCP Server instructions 的动态知识库说明。"""
    status = knowledge_base_status()
    if status["configured"] and status["readable"]:
        return (
            "知识库：用户已在控制台配置本地知识库。开始 PLC 编程前先调用 "
            "get_knowledge_base_status；根据任务关键词调用 search_knowledge_base"
            "（支持空格分隔多个关键词，优先返回全部命中的文件），再用 "
            "read_knowledge_document 按 first_match_offset 只读取相关命中文档。"
            "不得一次性遍历或读取整个知识库。"
            "用户当轮明确要求、当前 TIA 工程事实与编译结果优先于知识库；发生冲突必须说明。"
            "交付时列出实际参考的知识文件相对路径，未读取的文件不得声称已参考。"
        )
    return (
        "知识库：当前未配置可读的本地知识库；这是可选增强，相关工具返回 "
        "advisory=true、needs_user_action=false，只告知用户即可，不得当作阻塞，"
        "也不得猜测其中内容。若用户刚在控制台保存了知识库路径，提示其重新连接 "
        "MCP 服务或新建会话后再试。"
    )


def knowledge_base_log_line() -> str:
    """启动日志使用的一行知识库状态。"""
    status = knowledge_base_status()
    if not status["configured"]:
        return "[知识库] 未配置"
    if not status["readable"]:
        return f"[知识库] 已配置但目录不可用 -> {status['root']}"
    return f"[知识库] 根目录 {status['root']}"
