# Qwen Chat Coder Runtime

Qwen Chat Coder 的沙箱运行时环境（Runtime），基于 [OpenHands](https://github.com/All-Hands-AI/OpenHands) 架构改造。该服务运行在 Docker 容器内，为 AI 编程助手提供安全的代码执行、文件操作和浏览器交互能力。

## 架构概览

```
┌───────────────────────────────────────────────────────────────┐
│                      Docker Container                          │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │         Action Execution Server (FastAPI)                │  │
│  │                    Port: 35123                           │  │
│  │                                                          │  │
│  │  ┌─────────────────────────────────────────────────────┐ │  │
│  │  │          Sandbox Preview Proxy (/sandbox/*)          │ │  │
│  │  └─────────────────────────────────────────────────────┘ │  │
│  └──────────────┬──────────────────────────────────────────┘  │
│                 │                                             │
│      ┌──────────┼──────────┬──────────┬──────────┬───────┐   │
│      │          │          │          │          │       │   │
│      ▼          ▼          ▼          ▼          ▼       ▼   │
│  ┌───────┐ ┌───────┐ ┌────────┐ ┌────────┐ ┌───────┐ ┌─────┐│
│  │ Bash  │ │ File  │ │Browser │ │Jupyter │ │  MCP  │ │Proxy││
│  │Session│ │Editor │ │  Env   │ │ Plugin │ │ Proxy │ │→SB ││
│  │(tmux) │ │(OHEdi)│ │(Playw.)│ │(Tornado│ │(FastM)│ │     ││
│  └───────┘ └───────┘ └────────┘ └────────┘ └───────┘ └─────┘│
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │              File Viewer Server                          │  │
│  │           (localhost-only, no auth)                      │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
└───────────────────────────────────────────────────────────────┘
         │
         │  HTTP Proxy (/sandbox/* → localhost:4000)
         ▼
┌─────────────────────────────────┐
│   Sandbox Preview Service       │
│   (Node.js/Express + Vite)      │
│   Port: 4000 (API)              │
│   Port: 4001 (Preview)          │
│                                 │
│   - React 代码实时预览           │
│   - 自动 npm install            │
│   - Vite HMR 热更新              │
└─────────────────────────────────┘
         ▲
         │  HTTP API (Action/Observation)
         ▼
┌─────────────────────┐
│  OpenHands Backend  │
│   (外部调度服务)     │
└─────────────────────┘
```

## 核心模块

### 1. Action Execution Server (`openhands/action_execution_server.py`)

系统的入口点。基于 FastAPI 构建的 HTTP 服务，负责：

- 接收来自 OpenHands Backend 的 Action 请求
- 路由到对应的执行器（Bash/File/Browser/IPython）
- 返回 Observation 结果
- 提供文件上传/下载、目录列表等辅助 API

主要 API 端点：


| 端点                         | 方法   | 说明                        |
| -------------------------- | ---- | ------------------------- |
| `/execute_action`          | POST | 执行 Action 并返回 Observation |
| `/alive`                   | GET  | 健康检查                      |
| `/server_info`             | GET  | 服务状态（运行时间、资源占用）           |
| `/upload_file`             | POST | 文件上传（支持 zip 递归解压）         |
| `/download_files`          | GET  | 文件/目录打包下载                 |
| `/list_files`              | POST | 列出目录文件                    |
| `/vscode/connection_token` | GET  | 获取 VSCode 连接 token        |
| `/sandbox_preview_url`     | GET  | 获取沙箱预览服务 URL 信息           |
| `/sandbox/*`               | ALL  | 沙箱服务代理（详见下方）              |


### Sandbox Preview Service 集成

通过 Python 服务的 `/sandbox/*` 代理接口访问独立的 Node.js 沙箱预览服务（端口 4000/4001）。

详见 [`openhands/runtime/sandbox_preview/README.md`](openhands/runtime/sandbox_preview/README.md)。


### 2. 事件系统 (`openhands/events/`)

采用 Action-Observation 模式的事件驱动架构：

**Actions（输入动作）：**


| Action                    | 说明                    |
| ------------------------- | --------------------- |
| `CmdRunAction`            | 执行 Shell 命令           |
| `FileReadAction`          | 读取文件内容                |
| `FileWriteAction`         | 写入文件                  |
| `FileEditAction`          | 编辑文件（基于 OHEditor）     |
| `IPythonRunCellAction`    | 执行 IPython/Jupyter 代码 |
| `BrowseURLAction`         | 访问 URL                |
| `BrowseInteractiveAction` | 浏览器交互操作               |
| `MCPAction`               | MCP 协议调用              |
| `AgentDelegateAction`     | 委托子 Agent             |
| `MessageAction`           | 消息传递                  |


**Observations（输出观察）：**


| Observation                 | 说明           |
| --------------------------- | ------------ |
| `CmdOutputObservation`      | 命令执行结果       |
| `FileReadObservation`       | 文件内容         |
| `FileWriteObservation`      | 写入确认         |
| `FileEditObservation`       | 编辑结果（含 diff） |
| `IPythonRunCellObservation` | Jupyter 执行结果 |
| `BrowserOutputObservation`  | 浏览器页面内容      |
| `ErrorObservation`          | 错误信息         |


**序列化（`events/serialization/`）：** 所有事件通过 `event_from_dict` / `event_to_dict` 进行 JSON 序列化/反序列化，用于网络传输。

### 3. Runtime 组件 (`openhands/runtime/`)

#### 3.1 Bash Session (`openhands/utils/bash.py`)

- 基于 `libtmux` 的持久化 Shell 会话
- 使用 `bashlex` 解析和拆分复合命令
- 支持命令超时控制和内存限制
- 维护工作目录状态

#### 3.2 Browser Environment (`openhands/runtime/browser/`)

- 基于 Playwright + BrowserGym 的浏览器自动化
- 使用多进程隔离浏览器实例
- 支持 DOM 提取、截图、交互操作
- 支持文件下载检测

#### 3.3 Plugin 系统 (`openhands/runtime/plugins/`)

可插拔的插件架构：


| 插件                  | 说明                                       |
| ------------------- | ---------------------------------------- |
| `JupyterPlugin`     | 基于 Tornado WebSocket 的 Jupyter kernel 服务 |
| `AgentSkillsPlugin` | 内置 Agent 能力（文件操作、代码仓库操作）                 |
| `VSCodePlugin`      | OpenVSCode Server 集成                     |


#### 3.4 MCP Proxy (`openhands/runtime/mcp/`)

- 基于 FastMCP 的 Model Context Protocol 代理
- 支持挂载到主 FastAPI 应用
- 提供 API Key 认证

#### 3.5 File Viewer Server (`openhands/runtime/file_viewer_server.py`)

- 独立的文件预览服务（仅监听 localhost）
- 无需认证，用于 IDE 内嵌预览

#### 3.6 Sandbox Preview Service (`openhands/runtime/sandbox_preview/`)

基于 Node.js/Express + Vite 的 React 代码实时预览服务。详见 [`openhands/runtime/sandbox_preview/README.md`](./openhands/runtime/sandbox_preview/README.md)。

### 4. 工具模块 (`openhands/utils/`)


| 模块                     | 说明               |
| ---------------------- | ---------------- |
| `bash.py`              | Bash 会话管理（tmux）  |
| `memory_monitor.py`    | 内存使用监控           |
| `system_stats.py`      | 系统资源统计           |
| `git_handler.py`       | Git 操作封装         |
| `runtime_init.py`      | 运行时用户/目录初始化      |
| `shutdown_listener.py` | 优雅关闭监听           |
| `file_viewer.py`       | 文件预览 HTML 生成     |
| `edit.py`              | 文件编辑工具           |
| `async_utils.py`       | 异步辅助（同步转异步、并发等待） |


### 5. 存储抽象 (`openhands/storage/`)

- `FileStore` 抽象基类：定义文件读写、列表、删除接口
- 供上层模块统一调用文件系统操作

## 技术栈


| 类别       | 技术                            |
| -------- | ----------------------------- |
| Web 框架   | FastAPI + Uvicorn             |
| Shell 执行 | libtmux + bashlex             |
| 浏览器自动化   | Playwright + BrowserGym       |
| Jupyter  | Tornado WebSocket             |
| MCP 协议   | FastMCP                       |
| 文件编辑     | openhands-aci (OHEditor)      |
| 容器基础     | Python 3.12 (Debian Bookworm) |
| CI/CD    | AoneCI → ACR 镜像仓库             |


## 部署

### 本地开发启动

使用统一启动脚本同时启动 Python 和 Node.js 服务：

```bash
# 默认端口配置（Python:35123, Sandbox:4000, Preview:4001）
./start_services.sh

# 自定义端口
PYTHON_PORT=8080 SANDBOX_API_PORT=5000 PREVIEW_PORT=3000 ./start_services.sh
```

启动脚本会：
1. 启动 Node.js Sandbox Preview Service（端口 4000）
2. 启动 Python Action Execution Server（端口 35123）
3. 自动等待服务就绪
4. Ctrl+C 时优雅关闭所有服务

### Docker 构建

```bash
docker build -t qwen-chat-runtime .
```

### 运行

```bash
docker run -p 35123:35123 qwen-chat-runtime
```

容器启动命令：

```
python openhands/action_execution_server.py --no-enable-browser --working-dir /workspace 35123
```

### 环境变量


| 变量                          | 说明          | 默认值      |
| --------------------------- | ----------- | -------- |
| `SESSION_API_KEY`           | API 认证密钥    | 无（不启用认证） |
| `RUNTIME_MAX_MEMORY_GB`     | 最大内存限制 (GB) | 无限制      |
| `RUNTIME_MEMORY_MONITOR`    | 启用内存监控      | `False`  |
| `NO_CHANGE_TIMEOUT_SECONDS` | 令无输出超时     | `80`     |
| `LOCAL_RUNTIME_MODE`        | 本地运行模式      | `0`      |
| `INIT_PLUGIN_TIMEOUT`       | 插件初始化超时 (秒) | `120`    |
| `SANDBOX_SERVICE_URL`       | 沙箱服务 URL    | `http://localhost:4000` |
| `PREVIEW_HOST_PORT`         | 预览页面端口      | `4001`   |


## 目录结构

```
.
├── Dockerfile                  # 容器构建文件
├── requirements.txt            # Python 依赖
├── start_services.sh           # 统一启动脚本（Python + Node.js）
├── .aoneci/
│   └── openhands.yaml          # CI/CD 配置
└── openhands/
    ├── action_execution_server.py   # 主入口（FastAPI 服务）
    ├── core/                    # 核心模块
    │   ├── exceptions.py        # 自定义异常
    │   ├── logger.py            # 日志配置
    │   ├── mcp_config.py        # MCP 配置
    │   └── schema/              # 枚举/Schema 定义
    ├── events/                  # 事件系统
    │   ├── action/              # Action 定义
    │   ├── observation/         # Observation 定义
    │   ├── serialization/       # 序列化/反序列化
    │   ├── event.py             # 事件基类
    │   ├── stream.py            # 事件流
    │   └── tool.py              # 工具调用元数据
    ├── runtime/                 # 运行时组件
    │   ├── browser/             # 浏览器环境
    │   ├── mcp/                 # MCP 代理
    │   ├── plugins/             # 插件系统
    │   │   ├── agent_skills/    # Agent 内置技能
    │   │   ├── jupyter/         # Jupyter kernel
    │   │   └── vscode/          # VSCode 集成
    │   ├── sandbox_preview/     # Sandbox Preview Service（Node.js）
    │   │   ├── server.js        # Express API 服务
    │   │   ├── src/             # 沙箱执行器
    │   │   ├── sandbox-image/   # Docker 模式镜像
    │   │   ├── package.json     # Node.js 依赖
    │   │   ├── API.md           # Sandbox API 文档
    │   │   └── README.md        # Sandbox 说明文档
    │   └── file_viewer_server.py # 文件预览服务
    ├── storage/                 # 存储抽象
    └── utils/                   # 工具模块
        ├── bash.py              # Bash 会话（tmux）
        ├── memory_monitor.py    # 内存监控
        ├── system_stats.py      # 系统统计
        └── vscode-extensions/   # VSCode 扩展
```

## 数据流

```
Backend Request
       │
       ▼
[POST /execute_action]
       │
       ▼
event_from_dict(action_dict) → Action 对象
       │
       ▼
ActionExecutor.run_action(action)
       │
       ├─ CmdRunAction      → BashSession.execute()      → CmdOutputObservation
       ├─ FileReadAction     → file read / OHEditor.view  → FileReadObservation
       ├─ FileWriteAction    → file write                 → FileWriteObservation
       ├─ FileEditAction     → OHEditor.edit              → FileEditObservation
       ├─ IPythonRunCellAction → JupyterPlugin.run()      → IPythonRunCellObservation
       └─ BrowseURLAction    → BrowserEnv.browse()        → BrowserOutputObservation
       │
       ▼
event_to_dict(observation) → JSON Response
       │
       ▼
Backend Response
```

## 测试用例

### 1. 服务启动与健康检查

```bash
# 1.1 启动所有服务
./start_services.sh

# 1.2 Python 服务健康检查
curl http://localhost:35123/alive
# 预期: {"status":"ok"}

# 1.3 获取服务信息
curl http://localhost:35123/server_info
# 预期: 返回 uptime、idle_time、resources 信息
```

### 2. Action 执行 - Shell 命令

```bash
# 2.1 执行简单命令
curl -X POST http://localhost:35123/execute_action \
  -H "Content-Type: application/json" \
  -d '{"action":{"action":"run","args":{"command":"echo hello","is_static":false}}}'
# 预期: 返回 CmdOutputObservation，content 包含 "hello"

# 2.2 执行带工作目录的命令
curl -X POST http://localhost:35123/execute_action \
  -H "Content-Type: application/json" \
  -d '{"action":{"action":"run","args":{"command":"pwd","is_static":false}}}'
# 预期: 返回当前工作目录路径

# 2.3 执行静态命令（独立 bash session）
curl -X POST http://localhost:35123/execute_action \
  -H "Content-Type: application/json" \
  -d '{"action":{"action":"run","args":{"command":"echo static","is_static":true}}}'
# 预期: 返回 "static"，不影响主 session 状态

# 2.4 执行失败的命令
curl -X POST http://localhost:35123/execute_action \
  -H "Content-Type: application/json" \
  -d '{"action":{"action":"run","args":{"command":"exit 1","is_static":false}}}'
# 预期: 返回 exit_code=1 的 CmdOutputObservation
```

### 3. Action 执行 - 文件操作

```bash
# 3.1 写入新文件
curl -X POST http://localhost:35123/execute_action \
  -H "Content-Type: application/json" \
  -d '{"action":{"action":"write","args":{"path":"/tmp/test.txt","content":"line1\nline2\nline3"}}}'
# 预期: 返回 FileWriteObservation，path 为 /tmp/test.txt

# 3.2 读取文件
curl -X POST http://localhost:35123/execute_action \
  -H "Content-Type: application/json" \
  -d '{"action":{"action":"read","args":{"path":"/tmp/test.txt","impl_source":"oh_aci"}}}'
# 预期: 返回 FileReadObservation，content 包含 "line1\nline2\nline3"

# 3.3 读取二进制文件
curl -X POST http://localhost:35123/execute_action \
  -H "Content-Type: application/json" \
  -d '{"action":{"action":"read","args":{"path":"/bin/ls","impl_source":"oh_aci"}}}'
# 预期: 返回 FileReadObservation，content 为 "Binary File"

# 3.4 编辑文件（str_replace）
curl -X POST http://localhost:35123/execute_action \
  -H "Content-Type: application/json" \
  -d '{"action":{"action":"edit","args":{"path":"/tmp/test.txt","command":"str_replace","old_str":"line2","new_str":"modified_line2","impl_source":"oh_aci"}}}'
# 预期: 返回 FileEditObservation，包含 diff 信息

# 3.5 读取不存在的文件
curl -X POST http://localhost:35123/execute_action \
  -H "Content-Type: application/json" \
  -d '{"action":{"action":"read","args":{"path":"/tmp/nonexistent_file.txt","impl_source":"oh_aci"}}}'
# 预期: 返回 ErrorObservation
```

### 4. 文件上传与下载

```bash
# 4.1 上传单个文件
curl -X POST http://localhost:35123/upload_file \
  -F "file=@/tmp/test.txt" \
  -F "destination=/tmp/uploads"
# 预期: {"filename":"test.txt","destination":"/tmp/uploads","recursive":false}

# 4.2 上传 zip 文件（递归解压）
zip /tmp/test.zip /tmp/test.txt
curl -X POST http://localhost:35123/upload_file \
  -F "file=@/tmp/test.zip" \
  -F "destination=/tmp/unzipped" \
  -F "recursive=true"
# 预期: {"filename":"test.zip","destination":"/tmp/unzipped","recursive":true}

# 4.3 下载文件/目录（打包为 zip）
curl -o /tmp/download.zip http://localhost:35123/download_files?path=/tmp/uploads
# 预期: 下载成功，得到 zip 文件

# 4.4 下载不存在的路径（应 404）
curl http://localhost:35123/download_files?path=/tmp/nonexistent
# 预期: HTTP 404, {"detail":"File not found"}
```

### 5. 目录列表

```bash
# 5.1 列出目录文件
curl -X POST http://localhost:35123/list_files \
  -H "Content-Type: application/json" \
  -d '{"path":"/"}'
# 预期: 返回文件/目录列表

# 5.2 递归列出目录
curl -X POST http://localhost:35123/list_files \
  -H "Content-Type: application/json" \
  -d '{"path":"/tmp/uploads","recursive":true}'
# 预期: 返回递归的文件列表
```

### 6. Sandbox Preview 测试

Sandbox Preview 相关的测试用例（Session 生命周期、代码推送、WebSocket 日志、端到端流程）详见 [`openhands/runtime/sandbox_preview/README.md`](openhands/runtime/sandbox_preview/README.md#测试用例)。

## 安全机制

- API Key 认证（`X-Session-API-Key` Header）
- 文件预览服务仅监听 localhost
- Docker 沙箱隔离执行环境
- 工作目录权限管理（用户/组隔离）
- 内存使用监控与限制

