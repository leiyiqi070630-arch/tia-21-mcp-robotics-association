# device_templates/

放**你自己**的设备模板。

仓库从 2.3.0 起不再附带任何具体机型的 AML：模板取决于你的 CPU 型号、固件版本和本机 TIA 的
硬件支持包，别人给的模板换台机器不一定能导。所以这里只留一个空目录和一份说明。

## 怎么拿到 AML

1. **从已有工程导出（推荐）**

   连一个装过目标机型的工程，调用：

   ```
   export_project_aml(target_file="D:\\templates\\6ES7-212-1AG50-0XB0.aml")
   ```

   或者在 TIA Portal 里走「项目 → 导出 → CAx 数据」。导出的是标准 CAx(AML)，与机型无关的部分
   （订货号、模块清单、PROFINET 地址）都会带上。

2. **从同事/TIA 自带示例工程导出**

   只要那台机器上装过目标机型的硬件支持包就行。

## 怎么用

| 工具 | 用途 |
| --- | --- |
| `list_device_templates()` | 列出本目录的 `*.aml`，以及识别到的订货号、网络地址 |
| `add_device_from_template(order_number=..., ip_address=..., subnet_mask=..., confirm_import=true)` | 按订货号匹配 `*.aml`，新建设备并改写 PROFINET IP/掩码 |
| `add_device_from_aml(aml_path="D:\\templates\\xxx.aml", ...)` | 直接用绝对路径导入，不必放在本目录 |

匹配规则：只扫 `*.aml`，按订货号**前缀**比较（忽略空格、不区分大小写），所以 `6ES7 212-1AG50`
也能命中 `6ES72121AG500XB0`。文件名随便取，能让自己看懂就行。

目录为空时 `list_device_templates` 返回 `count: 0` 并给出提示，不会报错；
此时 `add_device_from_template` 会提示「模板目录中没有订货号 xxx 的 AML」。

## 不要删的文件

`group_marker.xml` 不是设备模板，是 `create_program_block_group` 创建程序块分组时导入的
内部标记块（见 `core/groups.py`）。删掉它会让分组工具直接报「缺少分组标记模板」。
