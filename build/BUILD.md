# 构建与打包（Cython + PyInstaller）

本目录只提供**脚本**，不包含任何自动打包流程；需要发布时手动执行。

## 产物形态

```
dist\tia-portal-v21-mcp\
├── tia-portal-v21-mcp.exe
└── _internal\                # Python 运行时、.pyd、siemens_tia_scripting 与 Openness 适配器
```

必须使用 **onedir**，不要改成 onefile。Openness 的防火墙授权与 EXE 的
路径/哈希绑定，onefile 每次启动都会解压到新的临时目录，导致每次连接都
重新弹授权窗口；onedir 的 EXE 路径固定，授权可以复用。

## 前置条件

- Windows 10/11 x64
- 项目根目录的 `.venv`（Python 3.12+；桌面控制台开发模式按这个名字找后端）
- MSVC Build Tools（Cython 编译 `.pyd` 需要 C 编译器）
- 构建依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

- 运行机器需要安装 **TIA Portal V21**（21.0 或 21.1）。本项目不会连接
  V15-V20；即使机器上装了旧版本，`detect_tia_installations` 也只会把它们
  放进 `blocked_installed`。

## 一键脚本

```powershell
# 完整流程：Cython -> PyInstaller
.\build.ps1

# 复用已经生成的 build\cython，只重跑 PyInstaller
.\build.ps1 -SkipCython
```

若系统执行策略禁止运行脚本（提示“未对文件进行数字签名”），改用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

## 分步执行

```powershell
# 1. Cython 编译 api/ 与 core/，staging 到 build\cython
.\.venv\Scripts\python.exe build\cython_build.py

# 2. PyInstaller（onedir）
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean build\tia_portal_v21_mcp.spec
```

## Cython 编译范围

| 路径 | 处理方式 |
| --- | --- |
| `mcp_run.py` | 保留为普通 `.py` 入口，便于定位启动问题 |
| `api/*.py`、`core/*.py` | 编译为 `build\cython\<pkg>\*.pyd` |
| `api/__init__.py`、`core/__init__.py` | 保留为普通 `.py` 包标记 |
| `device_templates/`、`build/assets/app-icon.ico` | 原样复制到 staging，再由 spec 收进 `_internal` |

`build\cython_build.py` 会在编译后启动一个独立 Python 进程导入
`api.mcp_api` 与 `core.tia_service`，确认扩展模块可加载；失败时脚本以
非零状态退出，不会继续打包。

## 打包后自检

```powershell
.\dist\tia-portal-v21-mcp\tia-portal-v21-mcp.exe
```

启动横幅应显示：

```
版本   v2.3.0
工具   72 个
支持   TIA Portal V21
限制   其它版本一律拒绝连接
机器人协会，欢迎交流工业自动化与 AI 技术
```

MCP 端点为 `http://127.0.0.1:8000/mcp`。若 8000 被占用，服务会打印占用者
排查命令并以退出码 2 结束，不会静默换端口。

## Openness 授权注意事项

- 首次连接，或 EXE 被移动/重新打包后，TIA Portal 会弹出 Openness 授权窗口；
  必须人工选择「是，全部接受」并确认 UAC，MCP 不会自动点击。
- 授权与 EXE 路径、修改时间、文件哈希相关；重新打包后需要重新授权。
- 当前用户必须属于 Windows 组 `Siemens TIA Openness`，否则连接会被拒绝；
  加组后需要注销并重新登录。
