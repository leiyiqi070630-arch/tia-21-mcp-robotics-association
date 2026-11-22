"""把 LAD 网络（TIA 生成的 FlgNet）组装成 OB 的 Openness XML。

为什么需要它：`write_lad_block` 原本只生成 SIMATIC SD（.s7dcl），而 SD 无法正确设置
OB 的「优化块访问」属性（实测：`{ S7_Optimized := ... }` 对 OB 会被静默忽略），
S7-1200 G2 的 OB 缺了它会直接编译报错。可靠做法是走 Openness XML 导入，
并且 **FlgNet 必须由 TIA 自己生成**（手写 FlgNet 正是此前把 Portal 弄崩的原因）。
"""

from __future__ import annotations

import re
from html import escape
from typing import Any

from core.tia_versions import TiaVersion, parse_tia_version

# 目标 OB 不存在时使用的默认骨架：OB1 / ProgramCycle / 优化块访问。
# 结构与 TIA 导出的 OB XML 保持一致，仅作占位，CompileUnit 会被整体替换。
_OB_SKELETON_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<Document>
  <Engineering version="{engineering_version}" />
  <SW.Blocks.OB ID="0">
    <AttributeList>
      <AutoNumber>true</AutoNumber>
      <HeaderAuthor />
      <HeaderFamily />
      <HeaderName />
      <HeaderVersion>0.1</HeaderVersion>
      <Interface>
        <Sections xmlns="http://www.siemens.com/automation/Openness/SW/Interface/v{interface_schema}">
          <Section Name="Input">
            <Member Name="Initial_Call" Datatype="Bool" Accessibility="Public" Informative="true" />
            <Member Name="Remanence" Datatype="Bool" Accessibility="Public" Informative="true" />
          </Section>
          <Section Name="Temp" />
          <Section Name="Constant" />
        </Sections>
      </Interface>
      <IsIECCheckEnabled>false</IsIECCheckEnabled>
      <MemoryLayout>Optimized</MemoryLayout>
      <Name>Main</Name>
      <Namespace />
      <Number>1</Number>
      <ProgrammingLanguage>LAD</ProgrammingLanguage>
      <SecondaryType>ProgramCycle</SecondaryType>
      <SetENOAutomatically>false</SetENOAutomatically>
    </AttributeList>
    <ObjectList>
      <MultilingualText ID="1" CompositionName="Comment">
        <ObjectList>
          <MultilingualTextItem ID="2" CompositionName="Items">
            <AttributeList>
              <Culture>zh-CN</Culture>
              <Text />
            </AttributeList>
          </MultilingualTextItem>
        </ObjectList>
      </MultilingualText>
      <SW.Blocks.CompileUnit ID="100" CompositionName="CompileUnits">
        <AttributeList>
          <NetworkSource>
            <FlgNet xmlns="http://www.siemens.com/automation/Openness/SW/NetworkSource/FlgNet/v{flgnet_schema}" />
          </NetworkSource>
          <ProgrammingLanguage>LAD</ProgrammingLanguage>
        </AttributeList>
      </SW.Blocks.CompileUnit>
      <MultilingualText ID="8" CompositionName="Title">
        <ObjectList>
          <MultilingualTextItem ID="9" CompositionName="Items">
            <AttributeList>
              <Culture>zh-CN</Culture>
              <Text>"Main Program Sweep (Cycle)"</Text>
            </AttributeList>
          </MultilingualTextItem>
        </ObjectList>
      </MultilingualText>
    </ObjectList>
  </SW.Blocks.OB>
</Document>
"""


def default_ob_skeleton(version: str | TiaVersion = "21.0") -> str:
    """返回目标 TIA 版本对应的最小 OB1 SimaticML 骨架。"""
    target = parse_tia_version(version)
    return _OB_SKELETON_TEMPLATE.format(
        engineering_version=target.engineering_version,
        interface_schema=target.interface_schema,
        flgnet_schema=target.flgnet_schema,
    )


# 向后兼容既有导入；新代码应调用 default_ob_skeleton(connected_version)。
DEFAULT_OB_SKELETON = default_ob_skeleton("21.0")

_FLGNET = re.compile(r"<FlgNet\b.*?</FlgNet>", re.S)
_COMPILE_UNIT = re.compile(
    r"<SW\.Blocks\.CompileUnit\b.*?</SW\.Blocks\.CompileUnit>", re.S
)
_LANGUAGE = re.compile(r"<ProgrammingLanguage>.*?</ProgrammingLanguage>", re.S)
_MEMORY_LAYOUT = re.compile(r"<MemoryLayout>.*?</MemoryLayout>", re.S)
_IEC_CHECK = re.compile(r"<IsIECCheckEnabled>.*?</IsIECCheckEnabled>", re.S)
_NAME = re.compile(r"<Name>.*?</Name>", re.S)

_NETWORK_TEXTS = """<MultilingualText ID="{comment_id}" CompositionName="Comment">
        <ObjectList>
          <MultilingualTextItem ID="{comment_item_id}" CompositionName="Items">
            <AttributeList>
              <Culture>zh-CN</Culture>
              <Text>{comment}</Text>
            </AttributeList>
          </MultilingualTextItem>
        </ObjectList>
      </MultilingualText>
      <MultilingualText ID="{title_id}" CompositionName="Title">
        <ObjectList>
          <MultilingualTextItem ID="{title_item_id}" CompositionName="Items">
            <AttributeList>
              <Culture>zh-CN</Culture>
              <Text>{title}</Text>
            </AttributeList>
          </MultilingualTextItem>
        </ObjectList>
      </MultilingualText>"""

_COMPILE_UNIT_TEMPLATE = """<SW.Blocks.CompileUnit ID="{uid}" CompositionName="CompileUnits">
        <AttributeList>
          <NetworkSource>
            {flgnet}
          </NetworkSource>
          <ProgrammingLanguage>LAD</ProgrammingLanguage>
        </AttributeList>
        <ObjectList>
          {texts}
        </ObjectList>
      </SW.Blocks.CompileUnit>"""


def extract_flgnet(block_xml: str) -> list[str]:
    """从导出的 LAD 块 XML 中取出全部 FlgNet 网络（保持原有顺序）。"""
    networks = _FLGNET.findall(block_xml)
    if not networks:
        raise ValueError(
            "参考块里没有 <FlgNet> 网络；请确认该块确实是用 LAD 编写并已成功编译。"
        )
    return networks


def _build_compile_units(
    networks: list[str], network_texts: list[dict[str, Any]] | None = None
) -> str:
    if not networks:
        raise ValueError("至少需要一个 LAD 网络。")
    units: list[str] = []
    # 从 100 起分配 ID，避开骨架里块级 Comment/Title 占用的 1、2、8、9
    next_id = 100
    for index, flgnet in enumerate(networks, 1):
        configured = network_texts[index - 1] if network_texts and index <= len(network_texts) else {}
        if not isinstance(configured, dict):
            raise ValueError(f"network_texts[{index - 1}] 必须是对象。")
        title = str(configured.get("title") or f"网络 {index}").strip()
        comment = str(
            configured.get("comment") or f"网络 {index}：执行用户要求的梯形图逻辑。"
        ).strip()
        texts = _NETWORK_TEXTS.format(
            comment_id=next_id + 1,
            comment_item_id=next_id + 2,
            title_id=next_id + 3,
            title_item_id=next_id + 4,
            comment=escape(comment, quote=False),
            title=escape(title, quote=False),
        )
        units.append(
            _COMPILE_UNIT_TEMPLATE.format(uid=next_id, flgnet=flgnet, texts=texts)
        )
        next_id += 10
    return ("\n      ").join(units)


def build_lad_ob_xml(
    skeleton_xml: str,
    networks: list[str],
    name: str,
    network_texts: list[dict[str, Any]] | None = None,
) -> str:
    """把骨架 OB XML 改造成：名称为 name、语言为 LAD、网络为 networks 的 OB。

    骨架里的其余属性（Number、SecondaryType、Interface 等）原样保留；
    并强制 `MemoryLayout` 为 Optimized —— S7-1200 G2 的 OB 缺它无法编译。
    """
    if not isinstance(skeleton_xml, str) or "<SW.Blocks.OB" not in skeleton_xml:
        raise ValueError("骨架必须是 TIA 导出的 OB（SW.Blocks.OB）XML。")
    if not name or not name.strip():
        raise ValueError("name 不能为空。")

    text = skeleton_xml

    matches = list(_COMPILE_UNIT.finditer(text))
    if not matches:
        raise ValueError("骨架里没有找到 SW.Blocks.CompileUnit，无法写入网络。")
    text = (
        text[: matches[0].start()]
        + _build_compile_units(networks, network_texts)
        + text[matches[-1].end() :]
    )

    # 此时只剩块级（AttributeList 里的）ProgrammingLanguage 需要改
    text = _LANGUAGE.sub("<ProgrammingLanguage>LAD</ProgrammingLanguage>", text)

    if _MEMORY_LAYOUT.search(text):
        text = _MEMORY_LAYOUT.sub("<MemoryLayout>Optimized</MemoryLayout>", text)
    elif _IEC_CHECK.search(text):
        text = _IEC_CHECK.sub(
            lambda m: m.group(0) + "\n      <MemoryLayout>Optimized</MemoryLayout>",
            text,
            count=1,
        )
    else:
        raise ValueError(
            "骨架里既没有 MemoryLayout 也没有 IsIECCheckEnabled，无法设定优化块访问。"
        )

    if _NAME.search(text):
        text = _NAME.sub(lambda m: f"<Name>{name}</Name>", text, count=1)
    else:
        raise ValueError("骨架里没有找到 <Name> 元素。")

    return text


def assert_lad_ob_xml(xml: str, name: str) -> None:
    """对组装结果做自检；任何一项不满足都说明不能交给 TIA。"""
    checks = {
        "块类型为 OB": "<SW.Blocks.OB" in xml,
        "语言为 LAD": "<ProgrammingLanguage>LAD</ProgrammingLanguage>" in xml,
        "保留优化块访问": "<MemoryLayout>Optimized</MemoryLayout>" in xml,
        "名称正确": f"<Name>{name}</Name>" in xml,
        "含 FlgNet 网络": "<FlgNet" in xml,
    }
    failed = [label for label, ok in checks.items() if not ok]
    if failed:
        raise ValueError("组装出的 OB XML 自检未通过：" + "、".join(failed))
