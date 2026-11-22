# TIA MCP 控制台（Electron）

本目录是 MCP 服务的 Windows 桌面控制台前端源码：启动使用声明、侧导航、服务启停、访问统计与历史曲线、推理偏好、明暗主题和实时日志。

## 目录

```
desktop/
├── package.json                 # electron 45.3.3 + electron-builder 26.18.0
├── src/
│   ├── main/
│   │   ├── main.js              # 主进程：窗口、IPC、5 秒轮询、退出清理
│   │   ├── config.js            # 路径解析（dev / 打包）、端口、日志目录
│   │   ├── service.js           # MCP 子进程启停（spawn / taskkill）
│   │   └── stats.js             # HTTP 采集 + 账本兜底 + 日志 tail
│   ├── preload/preload.js       # contextBridge 暴露 window.consoleAPI
│   └── renderer/
│       ├── index.html           # 侧导航、主页、推理设置、管理设置
│       ├── styles.css           # 明亮 / 暗黑主题
│       ├── app.js               # 渲染与交互
│       ├── chart.js             # canvas 折线/面积图（无第三方图表库）
│       └── assets/              # 运行时图标
├── resources/mcp-info.json      # 打包期缓存：版本 + 工具数量（--info 生成）
└── build/icon.ico               # 多尺寸 exe 图标（由 build/assets/tia-logo-source.png 生成）
```

## 开发运行

```powershell
cd desktop
$env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"   # 国内加速
npm install
npm start
```

开发态直接调用项目根目录的 `.venv\Scripts\python.exe mcp_run.py`，无需先打包后端。

> Node 24 自带的 npm 11 默认不执行依赖的安装脚本，`npm install` 之后 `node_modules\electron\dist`
> 可能是空的。首次 `npm start` 时 electron 会自行补下载（终端显示 `Downloading Electron binary...`），
> 也可手动 `node node_modules\electron\install.js`，再 `node_modules\electron\dist\electron.exe --version`
> 确认是 `v45.3.3`。

> 如果 Electron 一启动就闪退，多半是 Chromium 无法在 `%APPDATA%` 创建 profile。控制台已把 profile/缓存固定在程序目录下的 `console-data\`，
> 相关代码见 `src/main/main.js` 顶部的 `app.setPath('userData', …)`。

## 与后端的契约

控制台依赖 MCP 服务里的五条附加能力（都在 `mcp_run.py` 中注册，走 Starlette 自定义路由，不需要 MCP 授权）：

| 接口 | 用途 |
| --- | --- |
| `GET http://127.0.0.1:8000/console/ping` | 健康检查，判断端口上是否为本服务 |
| `GET http://127.0.0.1:8000/console/stats?days=7\|30` | 今日/本次/累计调用、工具数、历史曲线 |
| `GET http://127.0.0.1:8000/console/health` | 进程状态与 Openness 程序集完整性 |
| `GET/POST http://127.0.0.1:8000/console/open-log-dir` | 由服务进程调用 `explorer.exe` 打开日志目录 |
| `python mcp_run.py --info` | 不启动服务，只打印 `{ok, version, tool_count}` |

数据来源优先级：实时接口 → `log\usage.json` 账本 → 打包期缓存的 `mcp-info.json`。

调用计数通过 MCP 中间件在 `tools/call` 上累加（`core/console_runtime.py`），不统计握手与 `tools/list`。

## 运行时数据

| 路径 | 内容 |
| --- | --- |
| `<程序目录>\log\YYYYMMDD-HHMMSS.log` | 每次启动服务新建一个日志文件 |
| `<程序目录>\log\usage.json` | 每日调用次数账本（原子写入） |
| `<程序目录>\console-data\` | Chromium profile 与缓存（可随时删除） |
| `<程序目录>\console-data\settings.json` | 主题、LAD/SCL 与 Main/FB-FC 偏好、连接输出模板路径、当前用户知识库根目录 |

偏好生效链路：`settings.json` →（控制台启动服务时）`TIA_MCP_PREFERRED_LANGUAGE` /
`TIA_MCP_PREFERRED_BLOCK` 环境变量 → `core/programming_policy.py` → 同时写进
`initialize` 的 instructions 与 `write_lad_block` / `write_program_blocks` / `prepare_lad_block` /
`get_default_programming_policy` 四个工具的描述。在推理设置页保存时，若服务正在运行会自动重启服务；
DSH 这类客户端会缓存首次连接的工具描述，开关改完仍按旧偏好生成时，请重新连接该 MCP 服务或新建会话。

输出模板生效链路：`settings.json` 的 `connectTemplate` → 控制台启动服务时的
`TIA_MCP_CONNECT_TEMPLATE` 环境变量 → `core/output_templates.py` → MCP instructions 与
`connect_tia` / `get_tia_status` / `get_output_template` 三个工具的描述。模板正文由
`get_output_template(kind="connect")` 每次调用时重新读文件，改内容无需重启；换文件路径会触发服务重启。

知识库生效链路：`settings.json` 的 `knowledgeBaseRoot` → 控制台启动服务时的
`TIA_MCP_KNOWLEDGE_BASE_ROOT` 环境变量 → `core/knowledge_base.py` → MCP instructions、
编程工具描述与 `get_knowledge_base_status` / `list_knowledge_documents` /
`search_knowledge_base` / `read_knowledge_document` 四个只读工具。所有读取都限制在所选根目录内；
更换目录会触发服务重启，未配置知识库不会阻塞普通编程流程。

关闭窗口会结束 MCP 服务；服务进程由 `taskkill /T /F` 递归终止。

## 打包

在项目根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build-release.ps1
```

两步：先 Cython + PyInstaller 产出 `realease_mcp\tia-portal-v21-mcp\`，再用 electron-builder 打包控制台并合并为 `realease\TIA MCP 控制台\`。

打包最后一步会生成“启动器”：把 electron-builder 产出的 `TIA MCP 控制台.exe` 改名为
`tia-mcp-console.exe`，再用 `build\launcher\Launcher.cs` 编译一个同名的小程序放在同一目录。
启动器负责设置 `ELECTRON_DISABLE_SANDBOX=1` 并附带 `--no-sandbox` 拉起主体——部分电脑
（如装了火绒等安全软件）上 Chromium 沙箱初始化会失败，进程在加载 main.js 之前就以
`0x80000003` 中止，直接双击会“没有任何反应”。

> 启动器还会清掉继承来的 `ELECTRON_RUN_AS_NODE`。某些 AI/终端工具链会把这个变量写进全局环境，
> 一旦被 Electron 主体继承，它就会把自己当 Node 运行、窗口不出现，表现为“双击没反应”。
> 这也是打包版必须用启动器、不要直接用 `electron.exe` 测试的原因。

> 首次双击时 Windows SmartScreen 会提示「Windows 已保护你的电脑」，点「更多信息」→「仍要运行」即可。

「打开日志目录」优先请求 MCP 服务的 `/console/open-log-dir`，由 Python 后端调用 `explorer.exe`
打开（比直接 spawn explorer 更稳定）；服务未运行时才回退到 `spawnExplorer(['/n,/e,', dir])`，再回退
`shell.openPath`。`spawnExplorer` 不再使用 `windowsHide:true`，避免部分系统创建出不可见窗口而
表现为“没有反应”。

> 若 `mcp\_internal` 里的 Openness 程序集被继承成 Low 完整性级别，.NET 会拒绝加载（`0x80131515`），
> 服务在调用 TIA 工具时会以 `0xE0434352` 退出。`build-release.ps1` 会自动把发布目录提升为
> Medium 并给出人工命令（见根目录 README 9.1）。


## 自检开关（环境变量）

| 变量 | 作用 |
| --- | --- |
| `MCP_CONSOLE_SCREENSHOT=<png>` | 启动后截图并退出（`MCP_CONSOLE_SCREENSHOT_DELAY` 控制延迟） |
| `MCP_CONSOLE_SCREENSHOT_PAGE=notice\|home\|config\|profile\|reasoning\|knowledge\|settings` | 指定自动截图页面；`knowledge` 定位推理设置里的知识库栏目，`config` 验证配置复制与提示，`profile` 用于作者悬浮卡片视觉回归 |
| `MCP_CONSOLE_SMOKE=1` | 自动点开关启动 → 切 7 天 → 停止，各截一张图到 `build\shots\` |
| `MCP_CONSOLE_AUTOSTART=1` | 启动即拉起 MCP 服务 |
| `MCP_CONSOLE_SHOT_DIR` | 冒烟截图输出目录 |
