"""把紧凑 LAD 规格转换为 TIA Portal V21 SIMATIC SD 文档。"""

from __future__ import annotations

import re
from typing import Any


NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
SECTIONS = {
    "input": "VAR_INPUT",
    "output": "VAR_OUTPUT",
    "inout": "VAR_IN_OUT",
    "static": "VAR",
    "temp": "VAR_TEMP",
}


def _name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError(f"{field} 必须是英文字母/下划线开头的 PLC 标识符。")
    return value


def _atom(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串。")
    value = value.strip()
    if any(character in value for character in "\r\n;,{}()"):
        raise ValueError(f"{field} 不能包含换行、分号、逗号或括号。")
    if value.startswith("%") or (
        value.startswith('"') and value[1:].startswith("%")
    ):
        raise ValueError(
            f"{field} 不能使用绝对地址 {value!r}：V21 的 SIMATIC SD 导入只接受 PLC 符号变量。"
            "请先在 PLC 变量表中定义符号（例如 启动），再用 \"启动\" 引用；"
            "局部变量请用 #变量名。"
        )
    return value


def _wire(value: Any, field: str) -> str:
    if value == "powerrail":
        return "wire#powerrail"
    return f"wire#{_name(value, field)}"


def _call(value: Any) -> str:
    if not isinstance(value, dict):
        raise ValueError("call 必须是对象。")
    block = _name(value.get("block"), "call.block")
    instance = value.get("instance")
    target = f'"{block}"'
    if instance is not None:
        target = f'"{_name(instance, "call.instance")}".{target}'
    parameters: list[str] = []
    for section, operator in (("inputs", ":="), ("outputs", "=>")):
        mapping = value.get(section, {})
        if not isinstance(mapping, dict):
            raise ValueError(f"call.{section} 必须是对象。")
        for parameter, expression in mapping.items():
            parameters.append(
                f"{_name(parameter, f'call.{section} 参数')} {operator} "
                f"{_atom(expression, f'call.{section}.{parameter}')}"
            )
    return f"{target}({', '.join(parameters)})"


def _parameters(value: Any, field: str) -> list[str]:
    """Render instruction parameters while keeping source injection out."""
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象。")
    parameters: list[str] = []
    for section, operator in (
        ("inputs", ":="),
        ("inouts", ":="),
        ("outputs", "=>"),
    ):
        mapping = value.get(section, {})
        if not isinstance(mapping, dict):
            raise ValueError(f"{field}.{section} 必须是对象。")
        for parameter, expression in mapping.items():
            parameters.append(
                f"{_name(parameter, f'{field}.{section} 参数')} {operator} "
                f"{_atom(expression, f'{field}.{section}.{parameter}')}"
            )
    return parameters


def _templates(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{field} 必须是非空对象。")
    rendered: list[str] = []
    for name, template_value in value.items():
        name = _name(name, f"{field} 参数")
        template_value = _atom(template_value, f"{field}.{name}")
        if any(character in template_value for character in '\",'):
            raise ValueError(f"{field}.{name} 不能包含引号或逗号。")
        rendered.append(f"{name} := {template_value}")
    return f'{{ S7_Templates := "{", ".join(rendered)}" }}\n            '


def _instruction(value: Any, field: str = "instruction") -> str:
    """Render a native LAD instruction box in SIMATIC SD syntax.

    User block calls are quoted by :func:`_call`; Siemens instructions are reserved
    identifiers and deliberately remain unquoted (for example MOVE, GT and TON).
    """
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象。")
    allowed = {"name", "instance", "inputs", "inouts", "outputs", "templates"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{field} 包含不支持的字段：{sorted(unknown)}")
    name = _name(value.get("name"), f"{field}.name")
    target = name
    instance = value.get("instance")
    if instance is not None:
        target = f'"{_name(instance, f"{field}.instance")}".{name}'
    parameters = _parameters(value, field)
    return f"{_templates(value.get('templates'), f'{field}.templates')}{target}({', '.join(parameters)})"


def _move(value: Any) -> str:
    if not isinstance(value, dict):
        raise ValueError("move 必须是对象。")
    unknown = set(value) - {"in", "out", "outputs", "templates"}
    if unknown:
        raise ValueError(f"move 包含不支持的字段：{sorted(unknown)}")
    source = _atom(value.get("in"), "move.in")
    outputs = value.get("outputs")
    if outputs is None:
        destination = _atom(value.get("out"), "move.out")
        rendered_outputs = {"out1": destination}
    else:
        if value.get("out") is not None:
            raise ValueError("move.out 与 move.outputs 不能同时使用。")
        if not isinstance(outputs, list) or not outputs:
            raise ValueError("move.outputs 必须是非空数组。")
        rendered_outputs = {
            f"out{index}": _atom(destination, f"move.outputs[{index - 1}]")
            for index, destination in enumerate(outputs, 1)
        }
    return _instruction(
        {
            "name": "Move",
            "inputs": {"in": source},
            "outputs": rendered_outputs,
            **({"templates": value["templates"]} if "templates" in value else {}),
        },
        "move",
    )


COMPARE_INSTRUCTIONS = {
    "==": "EQ_Contact",
    "=": "EQ_Contact",
    "eq": "EQ_Contact",
    "<>": "NE_Contact",
    "!=": "NE_Contact",
    "ne": "NE_Contact",
    ">=": "GE_Contact",
    "ge": "GE_Contact",
    "<=": "LE_Contact",
    "le": "LE_Contact",
    ">": "GT_Contact",
    "gt": "GT_Contact",
    "<": "LT_Contact",
    "lt": "LT_Contact",
}


def _compare(value: Any) -> str:
    if not isinstance(value, dict):
        raise ValueError("compare 必须是对象。")
    unknown = set(value) - {
        "operator",
        "left",
        "right",
        "data_type",
        "templates",
    }
    if unknown:
        raise ValueError(f"compare 包含不支持的字段：{sorted(unknown)}")
    operator = str(value.get("operator", "")).strip().casefold()
    try:
        instruction = COMPARE_INSTRUCTIONS[operator]
    except KeyError as exc:
        raise ValueError("compare.operator 必须是 ==、<>、>=、<=、> 或 <。") from exc
    data_type = value.get("data_type")
    templates = value.get("templates")
    if data_type is not None and templates is not None:
        raise ValueError("compare.data_type 与 compare.templates 不能同时使用。")
    if templates is None:
        if data_type is None:
            raise ValueError(
                "compare.data_type 为必填项，例如 HSC 计数值应使用 DInt。"
            )
        templates = {"SrcType": _atom(data_type, "compare.data_type")}
    return _instruction(
        {
            "name": instruction,
            "inputs": {
                "in1": _atom(value.get("left"), "compare.left"),
                "in2": _atom(value.get("right"), "compare.right"),
            },
            "templates": templates,
        },
        "compare",
    )


def _ctrl_hsc(value: Any) -> str:
    if not isinstance(value, dict):
        raise ValueError("ctrl_hsc 必须是对象。")
    input_names = (
        "hsc",
        "dir",
        "cv",
        "rv",
        "period",
        "new_dir",
        "new_cv",
        "new_rv",
        "new_period",
    )
    output_names = ("busy", "status")
    allowed = {"instance", *input_names, *output_names, "templates"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"ctrl_hsc 包含不支持的字段：{sorted(unknown)}")
    if value.get("instance") is None:
        raise ValueError("ctrl_hsc.instance 为必填项；CTRL_HSC 需要实例 DB。")
    if "hsc" not in value:
        raise ValueError("ctrl_hsc.hsc 为必填项。")
    inputs = {
        name: _atom(value[name], f"ctrl_hsc.{name}")
        for name in input_names
        if name in value
    }
    outputs = {
        name: _atom(value[name], f"ctrl_hsc.{name}")
        for name in output_names
        if name in value
    }
    return _instruction(
        {
            "name": "CTRL_HSC",
            "instance": value.get("instance"),
            "inputs": inputs,
            "outputs": outputs,
            **({"templates": value["templates"]} if "templates" in value else {}),
        },
        "ctrl_hsc",
    )


def _step(step: Any) -> str:
    if not isinstance(step, dict) or len(step) != 1:
        raise ValueError("每个 LAD 步骤必须是仅含一个操作的对象。")
    operation, value = next(iter(step.items()))
    if operation == "contact":
        return f"Contact( {_atom(value, 'contact')} )"
    if operation == "nc_contact":
        return f"I_Contact( {_atom(value, 'nc_contact')} )"
    if operation == "coil":
        return f"Coil( {_atom(value, 'coil')} )"
    if operation == "inverted_coil":
        return f"I_Coil( {_atom(value, 'inverted_coil')} )"
    if operation == "wire":
        return _wire(value, "wire")
    if operation == "call":
        return _call(value)
    if operation == "instruction":
        return _instruction(value)
    if operation == "move":
        return _move(value)
    if operation == "compare":
        return _compare(value)
    if operation == "ctrl_hsc":
        return _ctrl_hsc(value)
    raise ValueError(f"不支持的 LAD 操作：{operation}")


def _defined_wires(rung: Any) -> list[str]:
    """返回一个 rung 中由 wire 步骤定义的线名（powerrail 除外）。"""
    if not isinstance(rung, dict):
        return []
    steps = rung.get("steps")
    if not isinstance(steps, list):
        return []
    names: list[str] = []
    for step in steps:
        if isinstance(step, dict) and set(step) == {"wire"}:
            value = step["wire"]
            if isinstance(value, str) and value != "powerrail" and NAME.fullmatch(value):
                names.append(value)
    return names


def _validate_rung_connections(networks: list[Any]) -> None:
    """校验并联支路的 start/end，拦下会生成自反馈网络的写法。

    自反馈网络（OR 门输出经触点接回自身输入）会让 TIA 的
    LADSimpleAutoFormatter 对空栈 Pop() 抛 InvalidOperationException，
    进而使整个 Portal 崩溃。必须在生成阶段就拦下，不能等 TIA 去渲染它。

    已知安全写法（自保持/并联支路）：
        主 rung：steps 中含 {"wire": "W1"}，start 取默认 powerrail
        支路 rung：start 取默认 powerrail，end 指向 "W1"
    """
    defined: set[str] = set()
    for index, network in enumerate(networks, 1):
        rungs = network.get("rungs") if isinstance(network, dict) else None
        if not isinstance(rungs, list):
            continue
        for rung_index, rung in enumerate(rungs, 1):
            if not isinstance(rung, dict):
                continue
            label = f"network {index} rung {rung_index}"
            start = rung.get("start", "powerrail")
            end = rung.get("end")
            if start != "powerrail" and start not in defined:
                raise ValueError(
                    f"{label} 的 start={start!r} 既不是 powerrail，"
                    "也不是前序 rung 中用 wire 步骤定义过的线。"
                )
            if start != "powerrail" and end is None:
                raise ValueError(
                    f"{label} 从线 {start!r} 起，必须用 end 指定它汇合回哪条线。"
                )
            if end is not None and end == start:
                raise ValueError(
                    f"{label} 的 start 与 end 同为 {start!r}，会生成自反馈并联支路；"
                    "TIA 的 LAD 自动格式化器遇到这种网络会使 Portal 崩溃。"
                    "自保持应写成：支路 rung 的 start 取默认（powerrail），"
                    "end 指向主 rung 中由 wire 步骤定义的线。"
                )
            defined.update(_defined_wires(rung))


def render_lad_block(spec: dict[str, Any]) -> tuple[str, str]:
    """返回 (块名, .s7dcl 内容)；只生成已定义的 LAD 子集。"""
    if not isinstance(spec, dict):
        raise ValueError("spec 必须是对象。")
    name = _name(spec.get("name"), "name")
    kind = str(spec.get("kind", "FB")).upper()
    if kind not in {"FB", "FC"}:
        raise ValueError("kind 目前支持 FB 或 FC。")
    header = f'FUNCTION_BLOCK "{name}"' if kind == "FB" else f'FUNCTION "{name}" : Void'
    footer = "END_FUNCTION_BLOCK" if kind == "FB" else "END_FUNCTION"
    lines = [
        '{ S7_Optimized := "TRUE"; S7_PreferredLanguage := "LAD"; S7_Version := "0.1" }',
        header,
    ]
    interface = spec.get("interface", {})
    if not isinstance(interface, dict):
        raise ValueError("interface 必须是对象。")
    unknown = set(interface) - set(SECTIONS)
    if unknown:
        raise ValueError(f"不支持的 interface 分区：{sorted(unknown)}")
    if kind == "FC" and interface.get("static"):
        raise ValueError("FC 不支持 static 变量。")
    declared: set[str] = set()
    for section, keyword in SECTIONS.items():
        variables = interface.get(section, {})
        if not isinstance(variables, dict):
            raise ValueError(f"interface.{section} 必须是变量名到类型的对象。")
        if not variables:
            continue
        lines.append(f"    {keyword}")
        for variable, data_type in variables.items():
            variable = _name(variable, f"interface.{section} 变量")
            if variable.casefold() in declared:
                raise ValueError(f"接口变量重名：{variable}")
            declared.add(variable.casefold())
            lines.append(f"        {variable} : {_atom(data_type, f'interface.{section}.{variable}')};")
        lines.append("    END_VAR")
    networks = spec.get("networks")
    if not isinstance(networks, list) or not networks:
        raise ValueError("networks 必须是非空数组。")
    _validate_rung_connections(networks)
    for index, network in enumerate(networks, 1):
        if not isinstance(network, dict):
            raise ValueError(f"network {index} 必须是对象。")
        rungs = network.get("rungs")
        if not isinstance(rungs, list) or not rungs:
            raise ValueError(f"network {index} 的 rungs 必须是非空数组。")
        lines += ['    { S7_Language := "LAD" }', "    NETWORK"]
        for rung_index, rung in enumerate(rungs, 1):
            if not isinstance(rung, dict):
                raise ValueError(f"network {index} rung {rung_index} 必须是对象。")
            steps = rung.get("steps")
            if not isinstance(steps, list) or not steps:
                raise ValueError(f"network {index} rung {rung_index} 的 steps 必须是非空数组。")
            lines.append(f"        RUNG {_wire(rung.get('start', 'powerrail'), 'rung.start')}")
            lines.extend(f"            {_step(step)}" for step in steps)
            end = rung.get("end")
            lines.append(f"        END_RUNG{f' {_wire(end, "rung.end")}' if end else ''}")
        lines.append("    END_NETWORK")
    lines.append(footer)
    return name, "\n".join(lines) + "\n"
