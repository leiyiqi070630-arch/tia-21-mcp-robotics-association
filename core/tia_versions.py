"""TIA Portal V21 专用版本归一化与能力判定。

本文件是 **V21 专用重构** 的版本边界：只接受 TIA Portal V21
（Openness 21.0 / 21.1），V15-V20 及其它主版本一律在解析、工程推断和
连接之前显式拒绝，避免把工程建在错误版本上。

业务层不要散落 ``21``、``V21`` 等硬编码，统一调用这里的
``parse_tia_version`` / ``ensure_supported`` / ``SUPPORTED_VERSIONS``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


# 对外公布的支持范围。V21.1 仍属于 V21 家族（Openness 21.1 适配器）。
SUPPORTED_MAJOR = 21
SUPPORTED_MINORS = (0, 1)
SUPPORTED_DESCRIPTION = "TIA Portal V21（Openness 21.0 / 21.1）"
UNSUPPORTED_MESSAGE = (
    "本项目仅支持 " + SUPPORTED_DESCRIPTION + "；{label} 已被禁止连接。"
    "请改用 TIA Portal V21，或用 V21 打开/另存工程后重试。"
)

# 显式版本写法（V21、21.0、TIA Portal V21.1）。允许 1-2 位主版本，
# 以便把 V16/V22 等请求交给 ensure_supported 给出“仅支持 V21”的明确错误，
# 而不是含糊的“无法识别”。
_VERSION_TEXT = re.compile(
    r"(?i)(?<![\d.])(?:tia\s*portal\s*)?v?\s*(\d{1,2})(?:[._]\s*(\d{1,2}))?"
)
_PROJECT_EXTENSION = re.compile(
    r"(?i)\.(?:ap|zap|al|als)(\d{1,2}(?:_\d{1,2})?)$"
)
_ENGINEERING_VERSION = re.compile(
    r"<Engineering\s+version\s*=\s*[\"']V(\d{1,2}(?:\.\d{1,2})?)[\"']\s*/?>",
    re.IGNORECASE,
)


class UnsupportedTiaVersionError(ValueError):
    """请求的 TIA 版本不在 V21 专用项目允许的范围内。"""


@dataclass(frozen=True, order=True)
class TiaVersion:
    """一个受支持的 TIA Portal/Openness 版本。"""

    major: int
    minor: int = 0

    @property
    def api_version(self) -> str:
        return f"{self.major}.{self.minor}"

    @property
    def label(self) -> str:
        if self.minor:
            return f"V{self.major}.{self.minor}"
        return f"V{self.major}"

    @property
    def engineering_version(self) -> str:
        """SimaticML ``<Engineering version="...">`` 使用的写法。"""
        return self.label

    @property
    def adapter_suffix(self) -> str:
        return f"{self.major}_{self.minor}" if self.minor else str(self.major)

    @property
    def interface_schema(self) -> int:
        # V17 起 STEP 7 Interface/FlgNet 模型为 v5，V21 继续沿用。
        return 5

    @property
    def flgnet_schema(self) -> int:
        return 5

    @property
    def supports_simatic_sd_lad(self) -> bool:
        # V21 全系列支持紧凑 LAD -> SIMATIC SD；保留属性便于既有调用方。
        return True

    @property
    def online_target_kind(self) -> str:
        # siemens_tia_scripting 的 V21 适配器把第三参数解释为 PLC 接口名
        # （例如 "1 X1"），不是 IP 地址。
        return "device_interface"

    def capabilities(self) -> dict[str, Any]:
        return {
            "version": self.api_version,
            "label": self.label,
            "engineering_version": self.engineering_version,
            "simaticml": True,
            "scl_external_source": True,
            "simatic_sd_lad": True,
            "lad_write": "compact_lad_auto",
            "online_target_kind": self.online_target_kind,
            "online_target_example": "1 X1",
            "interface_schema": self.interface_schema,
            "flgnet_schema": self.flgnet_schema,
            "supported": True,
        }


SUPPORTED_VERSIONS = tuple(
    TiaVersion(SUPPORTED_MAJOR, minor) for minor in SUPPORTED_MINORS
)
_SUPPORTED_BY_TUPLE = {(item.major, item.minor): item for item in SUPPORTED_VERSIONS}


def is_supported_version(version: TiaVersion) -> bool:
    """判断版本是否属于本项目允许的 V21 家族。"""
    return (version.major, version.minor) in _SUPPORTED_BY_TUPLE


def _coerce_raw(major: int, minor: int = 0) -> TiaVersion:
    """构造版本对象但不做支持性检查（供探测/诊断使用）。"""
    return TiaVersion(int(major), int(minor))


def _parse_raw(value: str | TiaVersion) -> TiaVersion:
    """宽松解析版本文本，保留 V15-V20 等未支持版本供诊断。"""
    if isinstance(value, TiaVersion):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"TIA 版本不能为空；本项目仅支持 {SUPPORTED_DESCRIPTION}。"
        )
    match = _VERSION_TEXT.search(value.strip())
    if match is None:
        raise ValueError(
            f"无法识别 TIA 版本 {value!r}；本项目仅支持 {SUPPORTED_DESCRIPTION}，"
            "可写 21、21.0、21.1 或 V21。"
        )
    return _coerce_raw(int(match.group(1)), int(match.group(2) or 0))


def ensure_supported(version: TiaVersion) -> TiaVersion:
    """版本不在 V21 家族时抛 ``UnsupportedTiaVersionError``。"""
    if is_supported_version(version):
        return version
    raise UnsupportedTiaVersionError(
        UNSUPPORTED_MESSAGE.format(label=version.label)
    )


def peek_tia_version(value: str | TiaVersion) -> TiaVersion | None:
    """尽力解析版本，无法识别时返回 ``None``（不校验是否受支持）。"""
    try:
        return _parse_raw(value)
    except (TypeError, ValueError):
        return None


def parse_tia_version(value: str | TiaVersion) -> TiaVersion:
    """把 ``V21``、``21.0``、``TIA Portal V21.1`` 归一化为受支持版本。

    非 V21 版本会抛 :class:`UnsupportedTiaVersionError`，调用方无需再额外
    判断主版本。
    """
    return ensure_supported(_parse_raw(value))


def normalize_requested_version(value: str | None) -> TiaVersion | None:
    """连接参数中的 auto/latest/空值返回 ``None``，其余返回 V21 版本。"""
    if value is None:
        return None
    normalized = value.strip().casefold().replace("_", "-")
    if normalized in {"", "auto", "automatic", "latest", "newest"}:
        return None
    return parse_tia_version(value)


def infer_project_version(path: str | Path) -> TiaVersion | None:
    """从 .ap/.zap/.al/.als 工程或库扩展名推断版本。

    扩展名带非 V21 版本（如 ``.ap16``）时直接抛
    :class:`UnsupportedTiaVersionError`，在真正打开工程前阻断。
    """
    match = _PROJECT_EXTENSION.search(str(path).strip())
    if match is None:
        return None
    token = match.group(1).replace("_", ".")
    version = _parse_raw(token)
    if not is_supported_version(version):
        raise UnsupportedTiaVersionError(
            f"工程 {path!s} 的扩展名表示 {version.label}，已被禁止。"
            f"本项目仅支持 {SUPPORTED_DESCRIPTION}；请先用 TIA V21 打开并"
            "另存为 .ap21 工程后重试。"
        )
    return version


def newest_version(values: Iterable[str]) -> TiaVersion | None:
    """从产品版本文本中挑出最新的受支持 V21 版本。"""
    found: list[TiaVersion] = []
    for value in values:
        try:
            found.append(parse_tia_version(value))
        except ValueError:
            continue
    return max(found) if found else None


def adapter_version_from_name(name: str) -> TiaVersion | None:
    """解析 Openness 适配器文件名；非 V21 适配器返回 ``None``。"""
    match = re.search(
        r"(?i)OpennessApi(15_1|15|16|17|18|19|20|21_1|21)\.dll$", name
    )
    if match is None:
        return None
    version = _coerce_raw(
        *(
            int(part)
            for part in match.group(1).replace("_", ".").split(".", 1)
        )
    )
    return version if is_supported_version(version) else None


def available_adapter_versions(directory: str | Path) -> list[TiaVersion]:
    """读取 Python 包或打包目录中实际存在的 V21 Openness 适配器。"""
    root = Path(directory)
    if not root.is_dir():
        return []
    found: set[TiaVersion] = set()
    for path in root.glob("Siemens.TiaPortal.OpennessApi*.dll"):
        version = adapter_version_from_name(path.name)
        if version is not None:
            found.add(version)
    return sorted(found)


def extract_engineering_version(xml: str) -> TiaVersion | None:
    """读取 SimaticML ``Engineering version``（不做 V21 支持性校验）。"""
    match = _ENGINEERING_VERSION.search(xml)
    return _parse_raw(match.group(1)) if match else None


def assert_xml_import_compatible(xml: str, target: str | TiaVersion) -> None:
    """阻断非 V21 或高版本 SimaticML 写入当前 Portal。"""
    target_version = parse_tia_version(target)
    source_version = extract_engineering_version(xml)
    if source_version is None:
        raise ValueError(
            "SimaticML XML 缺少 <Engineering version=...>；无法安全判断目标版本兼容性。"
        )
    if not is_supported_version(source_version):
        raise UnsupportedTiaVersionError(
            f"SimaticML XML 由 {source_version.label} 导出，已被禁止导入。"
            f"本项目仅支持 {SUPPORTED_DESCRIPTION}；请在 V21 中重新导出模板。"
        )
    if source_version > target_version:
        raise ValueError(
            f"不能把 {source_version.label} 导出的 SimaticML XML 导入 "
            f"{target_version.label}。请在目标版本中重新导出模板，不要只改版本字符串。"
        )


def compatibility_matrix() -> list[dict[str, Any]]:
    """返回 V21 家族的能力矩阵（只含受支持版本）。"""
    return [version.capabilities() for version in SUPPORTED_VERSIONS]


def version_policy() -> dict[str, Any]:
    """返回机器可读的版本策略，供 detect/文档/测试复用。"""
    return {
        "supported": SUPPORTED_DESCRIPTION,
        "supported_api_versions": [
            version.api_version for version in SUPPORTED_VERSIONS
        ],
        "blocked": "V15-V20 及 V21 之外的任何 TIA Portal 版本",
        "enforced_at": [
            "parse_tia_version",
            "infer_project_version",
            "assert_xml_import_compatible",
            "connect_tia",
            "create_tia_project",
            "launch_tia",
        ],
        "message_example": UNSUPPORTED_MESSAGE.format(label="V16"),
    }
