# TIA Portal V21 MCP · 机器人协会版

> 让 AI 通过 MCP 协议操作 **TIA Portal V21**（博途）：连接、建工程、加设备、写 SCL/LAD、
> 编译下载、查硬件与诊断。当前版本 **2.3.0**，注册 **72** 个 MCP 工具，
> 附带一个 Electron 桌面控制台（`desktop/`）。

**许可** MIT · **Python** 3.14.10 · **TIA Portal** 仅 V21（Openness 21.0 / 21.1）· **平台** Windows 10/11 x64

本仓库由旧的 `tia_portal_mcp`（V15-V21 多版本）重构而来，只保留 V21 能力：
V15、V15.1、V16、V17、V18、V19、V20 以及其它主版本一律拒绝连接，不做降级、
不做版本猜测、不提供兼容分支。

**特点**

- **72 个工具**：连接与诊断、工程、设备与硬件、程序块、编译下载、工艺对象、库与交叉引用、PLC 安全、用户知识库。
- **破坏性操作有闸门**：删除、导入、写入、下载都要显式 `confirm_*`，默认先备份，写后回读校验。
- **做不到就停下来问人**：证书信任、机密组态密码、PG/PC 接口这类 Openness 没有 API 的步骤，返回结构化 `user_action`，AI 必须原样转述，不允许硬猜。
- **桌面控制台**：一键启停服务、调用趋势、实时日志、LAD/SCL 与块架构偏好、用户知识库目录。

> ⚠️ **安全提醒**：本项目会真实执行编译与下载。`download_to_plc` 默认要求显式传入
> `confirm_download=true` 与 `allow_physical_plc=true`；对实体 PLC 下载可能造成停机或数据丢失，
> 请先在离线工程和测试 CPU 上验证。详见第 14 节「许可与免责」。

## 目录

1. 版本限制（硬约束）—— 只支持 TIA Portal V21
2. 功能范围
3. 环境要求
4. 安装与启动（源码）—— 含依赖清单与西门子 wheel 安装
5. 客户端配置
6. 工具清单（72 个）
7. 典型工作流
8. 目录结构
9. 构建与打包
10. 常见问题
11. MCP 无法自动完成、需要人工介入的场景
12. 变更日志
13. 桌面控制台（Electron）
14. 许可与免责
15. 致谢

---

## 1. 版本限制（硬约束）

| 场景 | 行为 |
| --- | --- |
| `connect_tia(version="auto")` | 只在**已安装的 V21** 中选择（21.0 / 21.1）；一个都没有时报错，不会退回旧版本 |
| `connect_tia(version="16.0")` | 抛 `UnsupportedTiaVersionError`，提示「本项目仅支持 TIA Portal V21」 |
| `create_tia_project(version="V15.1")` | 同上，拒绝创建 |
| `launch_tia(version="20")` | 同上，拒绝启动 |
| 打开 `*.ap16` 工程 | 拒绝，并提示先用 TIA Portal 另存为 `.ap21` / `.ap21_1` |
| 导入 V16/V20 生成的 SimaticML | 拒绝，提示用 V21 重新导出 |
| 本机只装了 V16 | `detect_tia_installations` 把 V16 放进 `blocked_installed`，`connectable_versions` 为空 |
| 同时装了 V16 与 V21 | 只列 V21 为可连接；V16 始终是 `supported: false` |
| 同时装了 V21.0 与 V21.1 | 视为两个 V21 子版本，`auto` 取较新的 21.1；需要固定版本时显式传 `21.0` / `21.1` |
| 已经运行的 V16 实例 | 不会被 attach；只在结果里作为不可用实例出现 |

对外暴露的版本策略可直接查询：

```json
// get_tia_compatibility() / detect_tia_installations().policy
{
  "supported": "TIA Portal V21（Openness 21.0 / 21.1）",
  "supported_api_versions": ["21.0", "21.1"],
  "blocked": "V15 / V15.1 / V16 / V17 / V18 / V19 / V20 及其它主版本",
  "enforced_at": ["parse_tia_version", "normalize_requested_version", "infer_project_version", "adapter_version_from_name"],
  "message_example": "本项目仅支持 TIA Portal V21（Openness 21.0 / 21.1）；V16 已被禁止连接。"
}
```

---

## 2. 功能范围

- **连接管理**：检测安装、启动/复用 V21 实例、连接/断开、状态与能力查询。
- **工程管理**：创建工程、保存/另存、归档、工程文本导出导入、模块描述刷新。
- **设备与硬件**：AML(CAx) 导出、按模板添加设备并写 IP/掩码、硬件清单、属性查找/写入、S7-1200 HSC 组态与读取。
- **程序块**：程序块/变量表/UDT/监控表导入导出，SCL 与 SIMATIC SD 写入，LAD 规格生成与写入，删除（带备份）。
- **编译与下载**：硬件/软件编译、下载到 PLC、备份、在线/离线比较。
- **工艺对象**：**不兼容**——6 个工具仍注册着（保持接口稳定），但当前无法正常使用，见 6.7。需要工艺对象时请在 TIA Portal 中手工组态。
- **库与交叉引用**：全局库/项目库、主副本、全局库打开、交叉引用导出、块保护。
- **在线操作**：`go_online` / `go_offline`，V21 使用设备接口名（默认 `1 X1`），不是 IP。
- **用户知识库**：控制台选择每台电脑自己的根目录，MCP 在根目录内安全地列出、检索并分段读取文本资料。

---

## 3. 环境要求

| 项 | 要求 |
| --- | --- |
| 操作系统 | Windows 10/11 x64 |
| TIA Portal | **V21**（21.0 或 21.1），且安装了 Openness 组件 |
| Python（源码运行） | **3.14.10**（推荐版本），必须是 64 位。3.12 / 3.13 需装对应 cpXX 的 wheel |
| 西门子绑定包 | `siemens_tia_scripting` 1.4.3 —— **不在 PyPI，需手工装 wheel**，见 4.2 |
| 权限 | 当前用户属于 Windows 组 `Siemens TIA Openness`（加组后必须注销重登） |
| 端口 | 默认 `127.0.0.1:8000`；被占用时打印占用提示并退出（退出码 2） |
| Node.js（可选） | 20+，只有桌面控制台需要 |

首次连接会弹出 Openness 授权窗口，需要人工选择「是，全部接受」并确认 UAC；
MCP 不会自动点击。授权与 EXE/解释器路径相关，更换路径或重新打包后需要重新授权。

---

## 4. 安装与启动（源码）

需要三样东西：**Python 3.14.10**、**西门子官方 `siemens_tia_scripting` wheel**、本仓库代码。
西门子那个包不在 PyPI，所以 `requirements.txt` 里没有它，必须单独装（见 4.2）。

### 4.1 依赖清单

| 依赖 | 版本 | 怎么装 | 说明 |
| --- | --- | --- | --- |
| Python | **3.14.10** | python.org 安装包 | 必须 64 位；版本号决定装哪个 cpXX 的 wheel |
| `siemens_tia_scripting` | 1.4.3 | **手工装 wheel**，见 4.2 | 西门子 Openness 的 Python 绑定，不在 PyPI |
| `mcp[cli]` | `>=2,<3` | `pip install -r requirements.txt` | MCP Python SDK，会带入 starlette / pydantic / uvicorn / anyio / httpx |
| `pywin32` | `>=311` | 同上 | 只用于读 TIA 的版本号；缺失时该字段为 `null`，不影响连接 |
| TIA Portal | **V21**（21.0 / 21.1） | 西门子安装包 | 必须勾选 Openness；V15–V20 不支持 |
| Windows 用户组 | `Siemens TIA Openness` | 计算机管理 → 本地用户和组 | 加组后**注销并重新登录**才生效 |
| Node.js + Electron | Node 20+，electron 45.3.3 | `cd desktop; npm install` | 只有桌面控制台需要，纯 MCP 服务不需要 |
| 打包（可选） | Cython 3.1+、PyInstaller 6.16+ | `pip install -r requirements-dev.txt` | 只在自己打包时需要 |

> `requirements.txt` 里只写 PyPI 上能装到的包。删除、升级依赖时请保持这个原则，
> 否则 `pip install -r` 会在新机器上直接失败。

### 4.2 安装西门子 TIA Scripting Python（必做）

`siemens_tia_scripting` 是西门子随 Openness 发布的 Python 包，**不在 PyPI 上**。
（PyPI 上有一个同名包，是第三方实现，API 不同，不要装。）

1. 打开西门子工业支持下载页 **TIA Scripting Python**（条目号 **109742322**）：
   <https://support.industry.siemens.com/cs/ww/en/view/109742322>
2. 下载对应 TIA Portal V21 的压缩包，解压后是一组 wheel，文件名里带 Python 版本，
   例如 `siemens_tia_scripting-1.4.3-cp314-cp314-win_amd64.whl`。
3. **按你的 Python 版本挑 wheel**，挑错会报 `is not a supported wheel on this platform`：

   | 你的 Python | 要装的 wheel |
   | --- | --- |
   | 3.14.x | `...-cp314-cp314-win_amd64.whl` |
   | 3.13.x | `...-cp313-cp313-win_amd64.whl` |
   | 3.12.x | `...-cp312-cp312-win_amd64.whl` |

   不确定版本时先执行 `python -c "import sys; print(sys.version)"`。
4. 安装并自检：

   ```powershell
   .\.venv\Scripts\python.exe -m pip install "D:\下载\siemens_tia_scripting-1.4.3-cp314-cp314-win_amd64.whl"

   # 能打印出版本号（1.4.3）就说明装对了
   .\.venv\Scripts\python.exe -c "import importlib.metadata as m; print(m.version('siemens_tia_scripting'))"
   ```

> wheel 内含 `Siemens.TiaPortal.OpennessApi21.dll` 等西门子专有二进制，**不要提交进 Git**、
> 不要转发，也不受本仓库 MIT 许可约束（见第 14 节）。
> 代码里没有"解压到目录 + 设 PYTHONPATH"的回退路径，必须真正 `pip install`。

### 4.3 创建环境并启动

```powershell
# 在已克隆或解压的项目根目录中执行以下命令

# 1. 创建虚拟环境：必须放在项目根目录、并且命名为 .venv
#    （桌面控制台的开发模式就是按 .venv\Scripts\python.exe 启动后端的）
#    推荐 3.14.x；版本必须与 4.2 里挑的 wheel 对应
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. 装运行依赖（不含西门子 wheel）
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. 装西门子 wheel（见 4.2，这一步不能省）
.\.venv\Scripts\python.exe -m pip install "<你的 wheel 路径>"

# 4. 自检：不启动服务，只验证依赖是否装齐（新装环境建议先跑这条）
.\.venv\Scripts\python.exe mcp_run.py --info
#    期望输出：{"ok": true, "version": "2.3.0", "tool_count": 72}

# 5. 启动（streamable-http 传输）
.\.venv\Scripts\python.exe mcp_run.py
```

`--info` 能正常打印就说明 Python 依赖、`siemens_tia_scripting` 导入、72 个工具注册
都没问题；后面再连不上 TIA，问题就只在 TIA 安装、Openness 用户组或授权上，不用再怀
疑环境。

启动横幅：

```
版本   v2.3.0
接口   http://127.0.0.1:8000/mcp
工具   72 个
支持   TIA Portal V21
限制   其它版本一律拒绝连接
机器人协会，欢迎交流工业自动化与 AI 技术
```

启动前会自动检查以下两项，**全部通过后**才监听端口：

1. `127.0.0.1:8000` 是否可绑定；正常时显示“检测正常”。
2. 当前 Windows 用户是否属于 `Siemens TIA Openness` 组；已加入会显示当前用户与组名，
   未加入/无法确认会显示原因和处理步骤。

两项结果会放在同一个方格中，例如：

```text
┌──────────────────────────────────────────────────────────────┐
│  端口   127.0.0.1:8000 检测正常                              │
│  Openness 用户组：已加入（用户 admin）                       │
│  组名   Siemens TIA Openness                                 │
└──────────────────────────────────────────────────────────────┘
```

任一检查失败都会明确提示“MCP 服务未启动”，交互式窗口等待按键后退出（退出码 2），
不会闪退；非交互环境直接退出。

MCP 端点：`http://127.0.0.1:8000/mcp`。

---

## 5. 客户端配置

`mcp.json` / `settings.json` 示例（按客户端字段名调整）：

```json
{
  "mcpServers": {
    "tia-portal-v21": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

先启动 `mcp_run.py`，再让 AI 客户端连接。本仓库默认 HTTP 传输；如客户端要求
stdio 拉起进程，把 `mcp_run.py:474` 的 `transport="streamable-http"` 改成 `"stdio"` 即可
（改完无需其它配置，端点参数在 stdio 下不生效）。

连接后建议的第一句话：

> 先调用 `detect_tia_installations`，只允许连接 V21；如果没有 V21，直接告诉我，不要尝试其它版本。

---

## 6. 工具清单（72）

### 6.1 服务与诊断（7）

| 工具 | 说明 |
| --- | --- |
| `get_service_info` | 服务版本、PID、工具数量、运行时长、端点、已注册工具清单 |
| `check_openness_prerequisites` | 检查当前用户是否属于 `Siemens TIA Openness` 组，返回修复步骤 |
| `get_openness_access_guide` | 首次连接/更换 EXE 后的 Openness 安全授权窗说明 |
| `list_installed_tia_products` | 列出本机已安装的 TIA Portal 产品与版本 |
| `detect_tia_installations` | 汇总安装版本；只有 V21 进入 `supported`，其它进 `blocked_installed` |
| `list_running_tia_portals` | 探测当前运行的 TIA 实例（只读，不 attach） |
| `launch_tia` | 启动 V21 并可打开工程；已有实例默认复用 |

### 6.2 连接、策略与知识库（12）

| 工具 | 说明 |
| --- | --- |
| `connect_tia` | 连接 V21；`version` 只接受 `auto` / `21.0` / `21.1` |
| `disconnect_tia` | 断开连接；关闭 TIA 需 `confirm_close=true` |
| `get_tia_status` | 连接、工程、PLC 选择、在线状态 |
| `get_tia_compatibility` | 当前连接能力；未连接时返回 V21 能力矩阵与版本策略 |
| `select_plc` | 在多 PLC 工程中选择目标 PLC |
| `get_default_programming_policy` | 读取控制台设置的 LAD/SCL 与 Main/FB-FC 默认偏好 |
| `get_output_template` | 读取控制台「推理设置 → 输出模板」里配置的 Markdown 正文（`kind="connect"`）；连接任务收尾必读 |
| `get_knowledge_base_status` | 读取控制台选择的知识库根目录与可读状态；未配置时不阻塞编程 |
| `list_knowledge_documents` | 在知识库根目录内列出支持的文本资料，只返回安全相对路径 |
| `search_knowledge_base` | 按关键词检索知识资料并返回命中片段与相对路径 |
| `read_knowledge_document` | 按相对路径和字符区间分段读取资料，拒绝越出知识库根目录 |
| `get_project_creation_capabilities` | 目录建设备/AML 建设备/已有设备改硬件分别是否支持 |

### 6.3 工程（12）

| 工具 | 说明 |
| --- | --- |
| `create_tia_project` | 创建 V21 工程；`version` 只接受 `auto` / `21.0` / `21.1` |
| `export_project_aml` | 把当前工程导出为 CAx(AML)，可作为新建设备模板 |
| `add_device_from_aml` | 用 CAx(AML) 在当前工程新建设备，可写 PROFINET IP 与掩码 |
| `add_device_from_template` | 从 `device_templates/` 匹配 AML 新建设备并写 IP |
| `list_device_templates` | 列出 `device_templates/` 里的 AML、订货号、网络地址 |
| `get_device_configuration` | 当前工程的设备、PLC、HMI 摘要与可读属性 |
| `save_project` | 保存当前工程（`confirm_save=true`） |
| `save_project_as` | 另存为新工程（`confirm_save_as=true`） |
| `archive_project` | 归档当前工程，支持同名覆盖 |
| `update_module_descriptions` | 刷新模块描述；硬件目录更新后修复“模块不可用” |
| `export_project_texts` | 导出工程定义文本（注释/标题/描述），便于翻译 |
| `import_project_texts` | 把翻译后的工程文本导回工程（`confirm_import=true`） |

### 6.4 硬件（6）

| 工具 | 说明 |
| --- | --- |
| `get_hardware_inventory` | 列出设备、PLC、模块及静态硬件属性，可按名称片段过滤 |
| `find_hardware_property` | 全工程扫描硬件属性并返回可写候选 |
| `set_hardware_properties` | 写入动态硬件属性；失败默认回滚，需 `confirm_write=true` |
| `configure_s71200_hsc` | 配置 S7-1200 HSC（计数/频率/脉冲），未知型号返回候选映射 |
| `read_hsc_configuration` | 读取高速计数器硬件标识符、输入值、组态参数 |
| `list_windows_network_adapters` | 列出 Windows 网卡，用于 `go_online` 选择 PC 接口 |

### 6.5 程序与在线（13）

| 工具 | 说明 |
| --- | --- |
| `export_program_blocks` | 导出程序块；`block_names=null` 表示全部 |
| `import_program_blocks` | 从文件/目录导入程序块（`confirm_import=true`） |
| `write_program_blocks` | 用 SCL / SIMATIC SD / XML 文本写入程序块 |
| `delete_program_blocks` | 删除程序块；默认先备份，需 `confirm_delete=true` |
| `prepare_lad_block` | 把紧凑 LAD 规格转成 `.s7dcl`；`target_version` 只接受 V21 |
| `write_lad_block` | 生成并写入 LAD 块；未指定 kind/name 时按控制台块架构偏好补齐 |
| `compile_plc` | 编译 PLC 硬件/软件/两者 |
| `download_to_plc` | 编译并下载到 PLC；需处理下载确认与权限 |
| `backup_plc_software` | 备份程序块、变量表、UDT 与硬件组态 |
| `compare_plc_to_online` | 比较离线工程与在线 PLC |
| `get_plc_inventory` | 列出 PLC 程序块、变量表、UDT、工艺对象、监控表 |
| `go_online` | 连接 PLC；`target_interface` 填接口名（V21 默认 `1 X1`），不是 IP |
| `go_offline` | 把目标 PLC 切回离线 |

### 6.6 变量表 / UDT / 监控表（6）

| 工具 | 说明 |
| --- | --- |
| `export_plc_tag_tables` | 导出 PLC 变量表；`table_names=null` 表示全部 |
| `import_plc_tag_tables` | 从文件/目录导入变量表（`confirm_import=true`） |
| `export_udts` | 导出 PLC 用户数据类型（UDT） |
| `import_udts` | 从文件/目录导入 UDT（`confirm_import=true`） |
| `export_watch_tables` | 导出监控表 |
| `import_watch_tables` | 从文件/目录导入监控表（`confirm_import=true`） |

### 6.7 工艺对象（6）· ⚠️ 与 V21 不兼容

> 这 6 个工具仍然注册着（保持接口稳定、不破坏已有客户端配置），但**与 TIA Portal V21 不兼容**，
> 当前无法正常使用。需要工艺对象时请直接在 TIA Portal 里组态，不要依赖这些工具。

| 工具 | 说明 |
| --- | --- |
| `get_technology_object_details` | 查询工艺对象属性、路径一致性与支持的导出格式 |
| `export_technology_objects` | 导出为同版本 SimaticML 模板 |
| `import_technology_objects` | 用同版本模板创建/覆盖工艺对象（`confirm_import=true`） |
| `set_technology_object_properties` | 修改已公开属性，默认回读校验、失败回滚 |
| `compile_technology_objects` | 编译工艺对象；`object_names=null` 表示全部 |
| `delete_technology_objects` | 删除工艺对象，默认先备份（`confirm_delete=true`） |

### 6.8 库与交叉引用（6）

| 工具 | 说明 |
| --- | --- |
| `list_libraries` | 列出全局库、项目库与库类型 |
| `create_master_copy` | 把库类型复制到项目（`confirm_create=true`） |
| `create_global_library` | 新建全局库 `.al21`（`confirm_create=true`） |
| `open_global_library` | 打开全局库（`confirm_open=true`） |
| `export_cross_references` | 导出程序块交叉引用 |
| `set_block_protection` | 设置块保护/专有技术保护（`confirm_change=true`） |

### 6.9 PLC 防护与安全（3）

| 工具 | 说明 |
| --- | --- |
| `get_plc_security_settings` | 读取 PLC 上实际暴露的安全属性：访问控制、PUT/GET、Web 服务器、Syslog、安全事件；并列出不可达页面 |
| `set_plc_security_settings` | 写入白名单内的安全属性；离线、`confirm_write=true`、默认回读并编译硬件 |
| `get_plc_security_guide` | 不需要连接 TIA，返回「防护与安全」各页面与 Openness 可达性对照及人工步骤 |

**能自动配置的**（属性名随 CPU/固件可能不同，以 `present` 为准）：

| 键 | 对应属性 | 说明 |
| --- | --- | --- |
| `access_control` | `PlcAccessControlConfiguration` | `EnabledWithAccessControl` / `Disabled` |
| `put_get_communication` | `ProtectionEnablePutGetCommunication` | 允许 PUT/GET |
| `web_server` | `WebserverActivate` | Web 服务器开关 |
| `syslog_auto_accept_client` | `SysLogAutoAcceptClient` | 自动接受 Syslog 客户端 |
| `security_events_interval` | `ProtectionIntervalForSummarizeOfSecurityEvents` | 安全事件汇总间隔 |
| `security_events_high_load` | `ProtectionSummarizeSecurityEventsOnHighLoad` | 高负载时汇总 |
| `security_events_unit` | `ProtectionUnitForSummarizeSecurityEvents` | 汇总单位 |

**必须在 TIA 中人工配置的**：

- 保护机密的 PLC 组态数据（密码、下载附加保护）
- 访问等级 / 用户管理组态 / 管理员设置 / 匿名访问
- 证书管理器（导入/信任证书）
- Syslog 服务器列表与证书
- 与 TIA Portal/HMI 的通信模式（只读 `CommunicationMode`，枚举未公开）

写入安全属性后必须**编译硬件并下载**才在 PLC 生效；只做软件下载不会应用硬件安全配置。

### 6.10 程序块分组（1）

| 工具 | 说明 |
| --- | --- |
| `create_program_block_group` | 创建并命名程序块分组，支持 `A/B` 多级；必须离线、`confirm_create=true`；已存在时幂等返回 |

桥接层没有 `PlcBlockGroup.Groups.Create`，工具用「按目录层级导入空 FC 标记块 → 删除标记块」的路径创建分组；空分组会保留。创建后需 `save_project` 才会落盘。

---

## 7. 典型工作流

### 7.1 新建工程并写一段 LAD

> 仓库不带任何机型模板。要在工程里加设备，先用 `export_project_aml` 从含目标机型的工程导出 AML，
> 放进 `device_templates/`（或直接用 `add_device_from_aml` 指定路径），再用
> `add_device_from_template(order_number=...)`。详见 `device_templates/README.md`。

```
detect_tia_installations
→ create_tia_project(target_directory="D:\\plc", project_name="Demo", version="21.0", confirm_create=true)
→ get_default_programming_policy
→ prepare_lad_block(spec={...}, output_directory="D:\\plc\\lad", target_version="21.0")
→ write_lad_block(spec={...}, target_version="21.0")
→ compile_plc(scope="software")
→ save_project(confirm_save=true)
```

### 7.2 连接已有工程并在线

```
connect_tia(version="21.0", project_file_path="D:\\plc\\Demo.ap21", plc_name="PLC_1")
→ get_tia_status
→ go_online(connection_type="physical", network_adapter_name="Realtek ...", target_interface="1 X1")
→ compare_plc_to_online
→ go_offline
```

### 7.3 导入 SCL 并下载

```
connect_tia(version="auto")
→ write_program_blocks(blocks=[{"name": "FC_Test", "language": "SCL", "source": "..."}])
→ compile_plc(scope="software")
→ download_to_plc()
```

### 7.4 LAD 规格里的操作数（重要）

`prepare_lad_block` / `write_lad_block` 生成的是 V21 SIMATIC SD，LAD 操作数只接受：

- 局部变量：`#Enable`（FB/FC 接口变量）
- PLC 符号：`"输出_电机运行"`（先在 PLC 变量表里定义）
- 常量、带引号的 DB 变量等

**不支持绝对地址**（`%M0.0`、`%I0.0`、`"%M0.0"`）。V21 的 SD 导入会把裸 `%` 当语法错误，
把 `"%M0.0"` 当未定义符号，最终表现为「导入后没有生成程序块」。工具现在在生成阶段
直接拒绝并给出可执行提示：

```
SIMATIC SD 不支持绝对地址 '%M0.0'；请先在 PLC 变量表中定义符号（例如 启动），再用 "启动" 引用。
```

只有绝对地址时，先在 PLC 变量表里建符号，再在 LAD 规格里用 `"符号名"`。

### 7.5 读取/修改 PLC 安全设置

```
get_plc_security_guide
→ get_plc_security_settings
→ set_plc_security_settings(
      settings={"put_get_communication": true, "access_control": "EnabledWithAccessControl"},
      confirm_write=true)
→ compile_plc(scope="hardware")
→ download_to_plc(download_mode="hardware-software", allow_physical_plc=true, ...)
```

`get_plc_security_settings` 没返回的属性（机密组态密码、访问等级、用户管理、证书）
不要猜属性名；按返回的 `pages[].manual_steps` 在 TIA 中处理。

### 7.6 创建程序块分组

```
go_offline
→ create_program_block_group(group_path="通信/Modbus", confirm_create=true)
→ save_project(confirm_save=true)
```

分组创建后是空目录；当前版本没有把已有块移入分组的接口，块的位置仍由导入时的目录层级决定。

---

## 8. 目录结构

> 仓库里只有**源码与文档**。`.venv/`、`realease/`、`realease_mcp/`、`dist/`、`log/`、
> `console-data/`、`备份/`、`desktop/node_modules/`、`desktop/build/`（除 `icon.ico`）、
> `build/cython*/` 等都在 `.gitignore` 里排除，所以克隆下来约 1.5 MB。
> 规则是"白名单式"的（先忽略一切再逐项放行），新增源码目录时要同步改 `.gitignore`。

```
tia_v21_mcp/
├── mcp_run.py               # 启动入口：版本、指令、端口检查、横幅、控制台路由
├── api/
│   └── mcp_api.py           # 72 个工具注册与参数校验
├── core/
│   ├── tia_service.py       # 业务实现（连接/工程/硬件/程序/工艺对象）
│   ├── tia_versions.py      # V21-only 版本解析、拒绝策略、能力矩阵
│   ├── tia_extras.py        # 工程文本、库、交叉引用、块保护
│   ├── lad_builder.py       # 紧凑 LAD → SIMATIC SD
│   ├── ob_builder.py        # 默认 OB 骨架（V21 / schema v5）
│   ├── console_runtime.py   # 桌面控制台数据源：会话日志、访问计数、历史账本
│   └── pathing.py           # 源码/打包路径
├── desktop/                 # Electron 桌面控制台前端源码（见 desktop/README.md）
│   ├── src/main|preload|renderer/
│   ├── resources/mcp-info.json
│   └── build/icon.ico       # electron-builder 用的 exe 图标
├── device_templates/        # 放你自己的 AML 设备模板（见该目录 README.md）
├── build/                   # 只含打包脚本与图标源码，不含任何产物
│   ├── cython_build.py      # Cython 编译到 build/cython
│   ├── tia_portal_v21_mcp.spec
│   ├── make_icons.py        # build/assets 源 LOGO → 侧栏透明版 + 圆角白底 ico/png
│   ├── launcher/Launcher.cs # 分发用极简启动器（清 ELECTRON_RUN_AS_NODE、带 --no-sandbox）
│   ├── assets/              # 图标源文件（tia-logo-source.png、app-icon.ico）
│   └── BUILD.md
├── build.ps1                # 一键：Cython → PyInstaller（onedir）
├── build-release.ps1        # 两步发布：MCP 服务 → realease_mcp，再合并控制台 → realease
├── requirements.txt         # 运行依赖（不含西门子 wheel，见 4.2）
├── requirements-dev.txt     # 打包依赖
├── codex-mcp-config.toml    # MCP 客户端配置示例（Codex 格式）
├── LICENSE                  # MIT
├── .gitignore
├── .gitattributes
└── README.md
```

---

## 9. 构建与打包

仓库只提交**打包脚本和图标源码**，不提交任何产物：`dist/`、`realease/`、`realease_mcp/`、
`build/cython*/`、`build/pyinstaller/`、`desktop/dist/`、`desktop/build/`（除 `icon.ico`）都已在
`.gitignore` 里排除。也就是说这里给的是"怎么自己打"，不是"下载就能用"的成品 ——
需要现成程序请自行按本节构建。

只提供脚本，不自动打包：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\build.ps1
```

若系统执行策略禁止运行脚本，改用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

含两步：

1. `build\cython_build.py` 把 `api/*.py`、`core/*.py` 编译为 `.pyd`（staging 到 `build\cython`）。
2. `build\tia_portal_v21_mcp.spec` 用 PyInstaller 生成 **onedir** 目录。

产物：`dist\tia-portal-v21-mcp\tia-portal-v21-mcp.exe`。

不要改成 onefile：Openness 授权与 EXE 路径/哈希绑定，onefile 每次解压到新临时目录
会每次弹授权。详细说明见 [`build/BUILD.md`](build/BUILD.md)。

### 9.1 打包桌面控制台（两步发布）

需要额外的 Node.js（20+）+ 首次 `cd desktop; npm install`。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build-release.ps1
```

脚本会自动：清理 `realease_mcp`、`realease` 里的旧产物 → Cython → PyInstaller
（产物落在 `realease_mcp\tia-portal-v21-mcp\`）→ electron-builder 打包控制台 → 合并成
`realease\TIA MCP 控制台\` → 删掉 `build\cython`、`build\cython-temp`、`build\pyinstaller`、`dist`、`desktop\dist`。

| 参数 | 作用 |
| --- | --- |
| `-DesktopOnly` | 只重打控制台，复用上一步的 `realease_mcp` |

分发时把整个 `realease\TIA MCP 控制台\` 目录复制到目标 Win10/Win11 x64 电脑即可，
目录内 `mcp\` 是 MCP 服务、`log\` 存放运行日志、`console-data\` 在首次启动后自动生成。

> **第一次双击时 Windows 会弹「Windows 已保护你的电脑」**（SmartScreen 对未签名新程序的提示），
> 点「更多信息」→「仍要运行」即可，之后正常使用。
>
> 双击的 `TIA MCP 控制台.exe` 是一个极小的启动器（`build\launcher\Launcher.cs` 编译），
> 真正的 Electron 主体是同目录的 `tia-mcp-console.exe`。启动器会带上 `--no-sandbox` 并设置
> `ELECTRON_DISABLE_SANDBOX=1`，因为部分电脑（尤其装了第三方安全软件或带虚拟化环境）上 Chromium 沙箱初始化
> 会直接失败，进程在加载任何应用代码之前就以 `0x80000003` 退出，表现为“双击没反应”。
>
> 「打开日志目录」由 MCP 服务进程（Python 后端 `POST /console/open-log-dir`）调用
> `explorer.exe` 打开，控制台进程本身不再直接调 ShellExecute；服务未运行时才依次退回
> `spawnExplorer` 与 `shell.openPath`。这样绕开了个别安全软件拦截 ShellExecute 时弹出的
> 「Windows 无法访问指定设备、路径或文件」。
>
> **完整性级别**：打包脚本最后会检查 `realease\TIA MCP 控制台\mcp\_internal` 下的 Openness
> 程序集。如果这些文件继承了 Low 完整性级别（例如工程目录被沙箱或安全软件标记过），.NET
> Framework 会拒绝从「不受信任位置」加载程序集，报 `0x80131515`，表现为调用任何 TIA 工具时
> 服务直接以 `0xE0434352` 退出。脚本会尝试把发布目录提升为 Medium；仍失败时按提示在普通
> PowerShell 里执行：
>
> ```powershell
> icacls "realease\TIA MCP 控制台" /grant "$env:USERNAME:(OI)(CI)F" /T /C
> icacls "realease\TIA MCP 控制台" /setintegritylevel "(OI)(CI)Medium" /T /C
> ```
>
> 或者把整个目录复制到其它位置（如 `D:\TIA MCP 控制台`）再运行。


---

## 10. 常见问题

**Q：连接时报“未检测到已安装的 TIA Portal V21”？**
确认本机装的是 V21（不是 V20/V19），且 `C:\Program Files\Siemens\Automation\Portal V21`
存在。仅装 V16 的机器不会被允许连接。

**Q：`connect_tia(version="auto")` 报“多个 V21 子版本”？**
本机同时有 21.0 与 21.1。显式传 `version="21.0"` 或 `"21.1"`。

**Q：`go_online` 填 IP 报错？**
V21 使用设备接口名，默认 `1 X1`。用 `get_device_configuration` 或
`get_hardware_inventory` 确认接口名。

**Q：启动时端口 8000 被占用？**
会提示“端口被占用、请检查是否重复启动、当前 MCP 已停止运行”，并在交互式控制台
等待任意键（非交互环境直接以退出码 2 结束），不会静默换端口或闪退。

**Q：启动提示“未加入 Siemens TIA Openness 用户组”？**
按提示把当前 Windows 用户加入该组，然后**注销并重新登录 Windows**（只重启 MCP
不会刷新登录令牌），再启动 MCP。

**Q：Openness 授权窗口一直弹？**
确认 EXE/解释器路径没有变化；onefile 或每次重新打包都会导致重新授权。
选择「是，全部接受」并确认 UAC。

**Q：中文显示乱码？**
源码与文档均为 UTF-8；PowerShell 管道按 GBK 解码时可能显示乱码，用
`Get-Content -Encoding utf8` 或 VS Code 打开。

**Q：`cd desktop; npm install` 之后 `npm start` 说 Electron 没装好？**
Node 24 自带的 npm 11 默认不执行依赖的安装脚本，`node_modules\electron\dist` 会是空的。
不用手动修：第一次 `npm start` 时 electron 会自己补下载（终端显示 `Downloading Electron
binary...`，国内建议先设 `ELECTRON_MIRROR`，见第 13 节），下载完成后正常出窗口。
若补下载失败（公司网络/离线），手动执行一次：

```powershell
cd desktop
node node_modules\electron\install.js
node_modules\electron\dist\electron.exe --version   # 期望输出 v45.3.3
```

**Q：`npm start` 后进程起来了但没有窗口、几秒就退出？**
环境里被写入了 `ELECTRON_RUN_AS_NODE`（部分 AI/终端工具链会全局设置它），Electron 会
把自己当 Node 跑。先清掉再启动：

```powershell
Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
npm start
```

---

## 11. MCP 无法自动完成、需要人工介入的场景

TIA 的部分安全对话框只有人工能操作，Openness 没有对应 API。MCP 会把这些错误
转成结构化结果：`needs_user_action=true` + `user_action`（含 `code`、`title`、
`why_mcp_cannot_do_it`、`steps`、`retry`、`ai_instruction`）。

```json
{
  "ok": false,
  "error_type": "ValueError",
  "error": "Changing to online mode failed: 'The device is not trusted. Please check the certificate.'",
  "needs_user_action": true,
  "user_message": "TIA 需要人工信任 PLC 证书：1. 在 TIA 中打开当前工程；2. 在线 → 转到在线；……",
  "user_action": {
    "code": "trust_plc_certificate",
    "title": "TIA 需要人工信任 PLC 证书",
    "steps": ["在 TIA 中打开当前工程", "在线 → 转到在线", "..."],
    "retry": "用户确认已信任证书后，重新调用 go_online 或 download_to_plc。",
    "ai_instruction": "不要自动重试、猜测密码或尝试绕过 TIA 的安全机制；先向用户说明步骤，等用户确认后再重试一次。"
  }
}
```

| 场景 | `user_action.code` | MCP 为什么做不了 | 用户需要做什么 |
| --- | --- | --- | --- |
| 首次转在线提示证书未受信任 | `trust_plc_certificate` | Openness 没有信任证书的 API，TIA 对话框只能人点 | TIA「在线 → 转到在线」，选择实际网卡和目标接口，点击「信任」 |
| 硬件编译/下载提示机密组态密码 | `enter_plc_secret_password` | 密码只能用户输入，MCP 不猜测 | CPU 属性 → 保护与安全 → 机密 PLC 组态数据，设置密码；或用 `download_to_plc(..., plc_master_secret_password=...)` |
| PG/PC 接口名报 not found | `select_pc_interface` | TIA 接口名不是 Windows 别名 | 用 `list_windows_network_adapters` 的 `InterfaceDescription`，不要用 `WLAN` |
| 目标接口报 not found | `select_target_interface` | 接口名必须与 TIA 窗口一致 | S7-1200 G2 用 `1 X1`，不要填 IP |
| 下载/组态时 PLC 已在线 | `already_online` | Openness 不会自动切换在线状态 | 先 `go_offline`，完成后再 `go_online` |
| 下载报“连接到模块失败” | `check_download_connection` | 只能报告失败，无法代替人工排查 | 先验证 `go_online`、接口名、证书、IP/端口 102、是否被其它会话占用 |
| TIA 没有打开工程 | `open_project` | MCP 不会替用户新建/切换工程 | 打开 `.ap21` 或调用 `create_tia_project` |
| Openness 防火墙/UAC 授权 | `confirm_openness_authorization` | 安全授权不允许自动点击 | 在 TIA 窗口选择「是，全部接受」并确认 UAC |

**AI 行为约束**：只要结果里出现 `needs_user_action=true`，就必须停止自动重试，
把 `user_action.steps` 原样告诉用户并等待确认；不得声称已经完成，也不得尝试
猜密码、改证书或绕过 TIA 安全机制。用户确认后再重试**一次**。

---

## 12. 变更日志

### 2.3.0

- 推理设置页新增「输出模板」区域：可选择任意本地 Markdown 作为「连接博途输出模板」，
  支持预览、打开文件与清除；路径持久化到 `console-data/settings.json` 的 `connectTemplate`，
  启动 MCP 服务时以 `TIA_MCP_CONNECT_TEMPLATE` 注入。
- 新增只读工具 `get_output_template(kind="connect")`（这一步之后工具总数 68）：MCP 服务每次调用都重新读文件，
  修改模板正文无需重启；更换文件路径后控制台自动重启服务以载入新模板。
- `connect_tia`、`get_tia_status`、`get_output_template` 的工具描述前置【输出模板】提示；
  MCP instructions 增加一段硬规则——连接任务完成后先读模板，严格按模板章节、表格与字段输出，
  示例值必须替换为本次连接的真实值，模板不可读时如实说明并按默认格式汇报。
- 相关技能同步：`skill-tia-21-mcp` 新增「输出模板」硬规则并写入最短连接流程与交付报告，
  `skill-tia-21-programming` 明确连接模板不适用于编程交付。
- 预览折叠项在配置后自动显示（修复初版 `hidden` 未解除导致看不到模板内容）。
- 推理设置页新增「知识库」区域：可选择本地知识库根目录，路径持久化到
  `console-data/settings.json` 的 `knowledgeBaseRoot`，启动 MCP 服务时以
  `TIA_MCP_KNOWLEDGE_BASE_ROOT` 注入；保存前提示会重启 MCP 服务，清除时二次确认。
- 新增四个只读工具 `get_knowledge_base_status`、`list_knowledge_documents`、
  `search_knowledge_base`、`read_knowledge_document`（工具总数 72）：只在根目录内检索文本资料，
  不写入、不跟随符号链接越界；未配置或不可读时返回 `advisory=true`、
  `needs_user_action=false`，只告知不阻塞，Agent 按通用规范继续。
- 知识库检索支持空格分隔的多个关键词，优先返回全部命中的文件，并返回
  `matched_terms`、`first_match_offset`、`total_matches`、`truncated`；
  列表与检索结果补充截断、扫描上限和跳过文件数，便于 Agent 判断是否还有未返回内容。
- 相关技能同步：`skill-tia-21-mcp` 更新知识库检索顺序、生效与重连说明，工具速查改为 72 个；
  `skill-tia-21-programming` 统一「未配置/不可读/未命中/已参考」交付口径。

### 2.2.0

- 主页新增环境条：博途 V21 打开状态（5 秒探测一次）、当前用户是否属于
  `Siemens TIA Openness` 组（随首帧快照启动即检测）；未加入组时页面顶部给出四步操作指引。
- 控制台停止服务改为先调 `POST /console/shutdown` 优雅关停（先断开 TIA 连接），
  5 秒内没退出才回退 `taskkill /T /F`，避免把 TIA 侧的 Openness 会话打断。
- `connect_tia` 附加 TIA 失败时自动重试 3 次（间隔 1.5 秒）；仍失败且诊断里出现
  `TargetInvocationException` 时，错误文案提示「TIA 可能正在编译/保存/弹窗，等 10–30 秒再试」。
- 桌面控制台改为侧导航布局：主页保留数据看板、历史曲线和 MCP 服务开关，
  独立增加“推理设置”和“设置”页面。
- 推理设置支持“优先 LAD / SCL”和“优先 Main(OB1) / FB-FC”，配置持久化到
  `console-data/settings.json`，并在启动 MCP 服务时通过环境变量载入。
- MCP Server instructions、`get_default_programming_policy` 和 `write_lad_block` 默认块选择
  统一读取上述偏好；用户显式指定的语言和块架构仍然优先。
- 偏好现在还会写进 `write_lad_block` / `write_program_blocks` / `prepare_lad_block` /
  `get_default_programming_policy` 四个工具的描述（`tools/list` 返回的内容），不再只依赖
  `initialize` 的 instructions——很多客户端会缓存首次连接结果，只写 instructions 时模型看不到。
- 在推理设置页点“保存推理设置”时，如果 MCP 服务正在运行，控制台会**自动重启服务**换用新偏好；
  服务未运行则提示“下次启动时生效”。客户端若仍按旧偏好生成，请在 DSH 中重新连接该 MCP 服务或新建会话。
- 设置页增加明亮/暗黑主题、日志目录、当前日志和实时预览。
- 修复日志操作无响应：不再隐藏 Explorer 窗口；当前日志直接用关联程序打开，失败时回退记事本。

### 2.1.1

- **只支持 TIA Portal V21**（Openness 21.0 / 21.1）；V15-V20 在解析、连接、建工程、
  启动、工程推断、XML 导入各入口统一拒绝。
- `detect_tia_installations` 增加 `supported` / `blocked_installed` / `policy`；
  `installed_products` 每项增加 `supported`。
- 在线目标改为 V21 设备接口名语义（`1 X1`），传入 IP 会明确报错。
- 默认 LAD、OB 骨架、工艺对象版本门全部对齐 V21。
- 版本号提升到 `2.1.1`；服务名改为 `tia-portal-v21-mcp`。
- 新增 Cython 编译脚本、PyInstaller onedir spec、`build.ps1` 与构建文档。
- 测试重写为 V21 边界测试，覆盖各入口的版本拒绝与边界条件。
- **回归修复**：
  - LAD 规格中的绝对地址（`%M0.0`）在生成阶段直接拒绝并提示改用 PLC 符号，
    不再产生「导入后没有生成程序块」的难懂错误。
  - `compare_plc_to_online` 离线时返回 `error_code="not_online"`，不再误报「一致」。
  - `backup_plc_software` 改为逐对象导出，单个不一致块不再让整个备份失败。
  - 补齐 `create_global_library` / `open_global_library` 的缺失实现（此前调用会
    `AttributeError`）。
  - `detect_tia_installations` / `list_installed_tia_products` 的 `supported`
    只认 STEP 7 / TIA Portal 本体，不再把 S7-PLCSIM、Startdrive 误标为可连接。
  - `connect_tia` 发现 TIA 没有打开工程时会先 detach，避免残留附加会话。
  - `archive_project` 规范化 `.zap21` 扩展名，不再生成 `.zap21.zap21`。
  - `download_to_plc` 在纯软件下载时只编译软件，不再被无关的硬件组态错误挡住；
    新增 `plc_master_secret_password` / `module_read_password` /
    `module_write_password` 参数，供 S7-1200 G2 安全组态使用。
  - `download_to_plc` 支持在物理 PLC 上完成软件下载（stop → load → start），
    下载后在线比较一致。
  - 新增 `core/user_actions.py`：把证书未信任、机密组态密码、PC/目标接口名、
    已在线、下载连接失败、无工程、Openness 授权等 MCP 无法自动处理的场景转成
    `needs_user_action=true` + `user_action`，并在 MCP instructions 中要求 AI
    停止重试、原样告知用户。
  - 新增 PLC 防护与安全工具（+3，工具总数 67）：
    `get_plc_security_settings`、`set_plc_security_settings`、
    `get_plc_security_guide`。可读写 Openness 实际暴露的访问控制、PUT/GET、
    Web 服务器、Syslog、安全事件属性；机密组态密码、访问等级、用户管理、
    证书管理器明确标注为人工处理。
  - 新增程序块分组工具 `create_program_block_group`（+1，工具总数 67）：
    支持 `A/B` 多级命名、幂等创建；桥接层没有 Group.Create，工具用
    「嵌套目录导入空 FC 标记块 → 删除标记块」实现，空分组保留。
  - 启动端口冲突不再闪退：打印“端口被占用、请检查是否重复启动、当前 MCP 已停止”，
    并在交互式控制台等待任意键；非交互环境直接退出。同时修复 Windows 上
    `SO_REUSEADDR` 导致重复实例误判端口空闲的问题。
  - 启动前新增两项自动检查：8000 端口可绑定且 Openness 用户组已加入时，才启动
    MCP；成功时打印“端口检测正常”和当前用户/组名，失败时打印原因、处理步骤并
    “MCP 服务未启动”。

### 2.1.0（旧项目）

- 新增 6 个工艺对象工具；工程文本、库、交叉引用、块保护等 10 个工具。
- 支持 V15.1-V21.1 多版本。

---

## 13. 桌面控制台（Electron）

`desktop/` 是一个无系统边框的 Windows 控制台，把 MCP 服务当成可开关的后台进程管理：

- 开关按钮启停 MCP 服务（退出控制台会一并结束服务）；
- 「今日访问」「本次启动」「工具总数」「服务时长」四张统计卡，每 5 秒自动刷新；
- 「调用趋势」按天展示调用次数，可切换近 7 天 / 近 30 天；
- 「运行日志」实时 tail 当前会话日志，可一键打开日志目录或当前日志文件；
- 图标源图是 `build/assets/tia-logo-source.png`：侧栏直接用无背景 LOGO（暗色主题自动反白），窗口与 exe 用圆角白底版本（`build/make_icons.py` 生成 PNG 与多尺寸 ico）。

开发运行：

```powershell
cd desktop
npm install
npm start
```

打包见 [9.1](#91-打包桌面控制台两步发布)，源码细节与自检开关见 [`desktop/README.md`](desktop/README.md)。

控制台依赖 MCP 服务新增的七项能力（都不需要 MCP 授权）：

| 接口 | 说明 |
| --- | --- |
| `GET /console/ping` | 健康检查：端口上跑的是否为本服务 |
| `GET /console/stats?days=7\|30` | 今日/本次/累计调用、工具数、按天历史 |
| `GET /console/health` | 进程是否健康、Openness 程序集是否齐全 |
| `GET /console/environment` | 博途 V21 进程、Openness 用户组、程序集状态 |
| `GET/POST /console/open-log-dir` | 由服务进程在资源管理器中打开日志目录 |
| `POST /console/shutdown` | 先断开 TIA 再关停服务（控制台停止按钮走这条） |
| `python mcp_run.py --info` | 不启动服务，只打印 `{ok, version, tool_count}` |

主页顶部环境条每 5 秒探测一次：博途 V21 是否打开（有 `Siemens.Automation.Portal.exe` 就算运行中）、
当前用户是否在 `Siemens TIA Openness` 组；服务未启动时由控制台直接用 `tasklist`/`whoami` 探测，
服务启动后改用 `/console/environment`（Python 侧用 Toolhelp32 枚举）。

运行数据：`log\YYYYMMDD-HHMMSS.log`（每次启动一个新文件）、`log\usage.json`（每日调用账本）、
`console-data\`（Chromium profile，可随时删除）。调用计数由 `core/console_runtime.py` 的
MCP 中间件在 `tools/call` 上累加，不统计握手与 `tools/list`。

---

## 14. 许可与免责

**代码许可：MIT**，全文见 [LICENSE](LICENSE)。可以商用、修改、再分发，保留版权声明即可。

**许可范围**（不是仓库里的一切都归 MIT）：

| 内容 | 许可 | 说明 |
| --- | --- | --- |
| 本仓库自有的 Python / JS / PowerShell / C# 源码与文档 | **MIT** | 随便用 |
| `siemens_tia_scripting`（wheel） | 西门子专有 | **不在本仓库内**，需自行从西门子工业支持下载，使用受西门子许可约束 |
| `Siemens.TiaPortal.OpennessApi21.dll` 等 Openness 程序集 | 西门子专有 | 随 TIA Portal 安装，不随本仓库分发 |
| TIA Portal / SIMATIC / S7 / 博途 | 西门子商标 | 本项目与西门子无隶属关系，未获其背书 |
| Electron / Node.js / MCP Python SDK | MIT 等开源许可 | 由 `npm install` / `pip install` 从官方源获取，本仓库不重新分发 |

**免责条款**：

- 本项目**不是西门子官方产品**，未获西门子授权、背书或技术支持。
- 按 MIT 条款以 **"AS IS"（按原样）** 提供，不提供任何明示或暗示担保，包括适销性与特定用途适用性。
- 工具会真实执行编译与下载。**对实体 PLC 下载可能造成停机、数据丢失或设备损坏**，请先在离线工程和测试 CPU 上验证；一切后果由使用者自行承担。
- 服务默认只监听 `127.0.0.1:8000`，但它拥有完整的工程读写与下载能力。把它暴露到局域网、或交给不受信任的 AI 客户端之前，请自行评估风险与访问控制。
- 因使用本项目导致的任何直接或间接损失（含生产事故、数据丢失、许可纠纷），作者不承担责任。

**为什么不在 Gitee 放成品 exe**：Gitee 免费版限制单文件 50 MB、单仓库 500 MB，而桌面控制台成品
（Electron + PyInstaller onedir）约 250 MB，其中 `electron.exe` 一项就 238 MB，推送必然失败。
所以本仓库**只有源码**：想直接跑就用第 13 节的 `npm install` + `npm start`；想打包用第 9 节的脚本。

## 15. 致谢

- [Siemens TIA Scripting Python](https://support.industry.siemens.com/cs/ww/en/view/109742322) —— 没有它就没有这个项目
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) —— 服务端框架
- [Electron](https://www.electronjs.org/) / [electron-builder](https://www.electron.build/) —— 桌面控制台
- 以及在真实 PLC 上一起踩坑、验证过的同行

