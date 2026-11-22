"""PLC 编程偏好。

桌面控制台通过环境变量把用户选择传给 MCP 服务。这里集中完成校验与策略描述，
避免入口说明、工具返回值和服务能力页各自维护一套默认值。
"""

from __future__ import annotations

import os
from typing import Any


PREFERRED_LANGUAGE_ENV = "TIA_MCP_PREFERRED_LANGUAGE"
PREFERRED_BLOCK_ENV = "TIA_MCP_PREFERRED_BLOCK"

LANGUAGE_LAD = "LAD"
LANGUAGE_SCL = "SCL"
BLOCK_MAIN = "MAIN"
BLOCK_FB_FC = "FB_FC"


def _choice(value: str | None, allowed: set[str], fallback: str) -> str:
    normalized = str(value or "").strip().upper().replace("/", "_").replace("-", "_")
    return normalized if normalized in allowed else fallback


def programming_preferences() -> dict[str, str]:
    """读取并规范化当前进程的编程偏好。"""
    return {
        "language": _choice(
            os.environ.get(PREFERRED_LANGUAGE_ENV),
            {LANGUAGE_LAD, LANGUAGE_SCL},
            LANGUAGE_LAD,
        ),
        "block": _choice(
            os.environ.get(PREFERRED_BLOCK_ENV),
            {BLOCK_MAIN, BLOCK_FB_FC},
            BLOCK_MAIN,
        ),
    }


def programming_policy() -> dict[str, Any]:
    """返回给 MCP 客户端的结构化默认策略。"""
    preferences = programming_preferences()
    prefer_lad = preferences["language"] == LANGUAGE_LAD
    prefer_main = preferences["block"] == BLOCK_MAIN
    return {
        "language": preferences["language"],
        "language_label": "梯形图" if prefer_lad else "结构化控制语言",
        "primary_block": (
            {"name": "Main", "type": "OB", "number": 1}
            if prefer_main
            else {"name": None, "type": "FB/FC", "number": None}
        ),
        "comment_language": "zh-CN",
        "put_all_feasible_logic_in_ob1": prefer_main,
        "extension_policy": (
            "优先把可行逻辑写入 Main(OB1)；仅在安全性、正确性或可维护性需要时扩展 FB/FC/DB。"
            if prefer_main
            else "优先按功能拆分到 FB/FC（需要状态时使用 FB），并由 Main(OB1) 保留清晰调用入口。"
        ),
        "silent_language_fallback": False,
        "preferences": {
            "preferred_language": preferences["language"],
            "preferred_block": preferences["block"],
        },
    }


def programming_hint() -> str:
    """给工具描述用的一句话偏好提示。

    MCP 客户端每次拉取工具列表都会拿到描述，因此这行提示比 initialize 里的
    instructions 更容易被模型看到（很多客户端会缓存甚至忽略 instructions）。
    """
    policy = programming_policy()
    language = policy["language"]
    block = policy["preferences"]["preferred_block"]
    language_text = "LAD（梯形图）" if language == LANGUAGE_LAD else "SCL（结构化控制语言）"
    block_text = "Main(OB1)" if block == BLOCK_MAIN else "FB/FC 按功能拆分"
    return (
        f"【控制台偏好】默认语言 {language_text}，默认块架构 {block_text}；"
        "用户未明确指定时按此执行，改动前先调用 get_default_programming_policy 复核。"
    )


def programming_instruction() -> str:
    """生成注入 MCP Server instructions 的动态偏好说明。"""
    policy = programming_policy()
    language = policy["language"]
    prefer_main = policy["preferences"]["preferred_block"] == BLOCK_MAIN

    if language == LANGUAGE_LAD:
        language_text = (
            "用户没有指定编程语言时，优先使用 LAD（梯形图），网络标题和注释使用中文；"
            "不得在无提示时改成 SCL。只有 LAD 无法安全、正确地表达需求，或用户明确同意时，"
            "才使用 SCL，并说明原因。"
        )
    else:
        language_text = (
            "用户没有指定编程语言时，优先使用 SCL；不得在无提示时改成 LAD。"
            "只有 SCL 不适合当前任务，或用户明确要求时，才使用 LAD，并说明原因。"
        )

    if prefer_main:
        block_text = (
            "用户没有指定块架构时，把所有可安全维护的逻辑优先写入 Main(OB1)。"
            "只有逻辑无法在 OB1 安全、正确或可维护地完成时才扩展 FB/FC/DB，"
            "必须明确说明原因并由 OB1 调用。write_lad_block 的 spec 未给 kind/name 时"
            "会自动采用 OB/Main 并补中文网络注释。"
        )
    else:
        block_text = (
            "用户没有指定块架构时，优先按功能把逻辑写入 FB/FC：需要保持状态或实例数据时用 FB，"
            "无状态逻辑用 FC；Main(OB1) 只保留清晰的调用入口。write_lad_block 的 spec 未给"
            "kind/name 时会采用 FC/FC_MainLogic，实际任务中应按功能给块命名。"
        )

    return language_text + block_text
