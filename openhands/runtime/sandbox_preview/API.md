# Server3 - 单用户沙箱预览服务 API

单机单用户的代码预览沙箱服务，支持 Docker 容器和宿主机两种运行模式，推送文件后自动检测 `package.json` 变更并安装依赖。

## 基本信息

| 项目 | 值 |
|------|-----|
| API 端口 | `4000`（可通过 `PORT` 环境变量配置） |
| 预览端口 | `4001`（可通过 `PREVIEW_HOST_PORT` 配置） |
| 协议 | HTTP + WebSocket |
| Content-Type | `application/json` |
| Body 大小限制 | `5MB` |
| Session 模式 | 单 Session（创建新 Session 会自动销毁旧的） |

## 运行模式

通过环境变量 `SANDBOX_MODE` 切换：

| 模式 | 说明 |
|------|------|
| `docker`（默认） | 在 Docker 容器中运行 Vite，隔离性好 |
| `host` | 直接在宿主机 `/workspace` 目录运行 Vite 子进程，性能好、部署简单 |

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `4000` | API 服务监听端口 |
| `SANDBOX_MODE` | `docker` | 运行模式：`docker` 或 `host` |
| `SANDBOX_IMAGE` | `sandbox-vite` | Docker 模式使用的镜像名 |
| `PREVIEW_HOST_PORT` | `4001` | 预览服务的宿主端口 |
| `WORKSPACE_DIR` | `/workspace` | Host 模式下的工作目录 |

---

## 接口列表

### GET /health

健康检查。

**Response**

```
200 OK
```

---

### POST /session

创建（或重建）沙箱 Session。如果当前已有 Session，会先销毁旧 Session 再创建新的。

**Request**

无请求体。

**Response**

成功 (`201`):

```json
{
  "sessionId": "01j5x2abc3def4gh5ijk6lmnop",
  "status": "running",
  "previewUrl": "http://localhost:4001"
}
```

失败 (`500`):

```json
{
  "status": "error",
  "message": "<error detail>"
}
```

**Session 状态流转**

```
creating → starting → running / timeout
                    → error (启动异常)
```

---

### GET /session

获取当前 Session 状态。

**Response**

成功 (`200`):

```json
{
  "sessionId": "01j5x2abc3def4gh5ijk6lmnop",
  "status": "running",
  "previewUrl": "http://localhost:4001",
  "createdAt": 1716000000000
}
```

无活跃 Session (`404`):

```json
{
  "status": "error",
  "message": "No active session"
}
```

---

### PUT /session/files

向当前 Session 推送代码文件。如果推送的文件中包含 `package.json`，会自动在沙箱内执行 `npm install` 安装依赖。

**Request Body**

```json
{
  "files": {
    "package.json": "{\"dependencies\":{\"lodash\":\"^4.17.21\"}}",
    "src/App.jsx": "import _ from 'lodash';\nexport default function App() { return <h1>{_.camelCase('hello world')}</h1>; }"
  }
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `files` | `Record<string, string>` | 是 | key 为相对文件路径，value 为文件内容 |

**Response**

成功 (`200`) — 无需安装依赖:

```json
{
  "sessionId": "01j5x2abc3def4gh5ijk6lmnop",
  "status": "success",
  "filesWritten": ["src/App.jsx"],
  "install": null
}
```

成功 (`200`) — 包含 package.json，自动安装依赖:

```json
{
  "sessionId": "01j5x2abc3def4gh5ijk6lmnop",
  "status": "success",
  "filesWritten": ["package.json", "src/App.jsx"],
  "install": {
    "success": true,
    "output": "added 1 package in 2s"
  }
}
```

成功 (`200`) — 依赖安装失败（文件仍写入成功）:

```json
{
  "sessionId": "01j5x2abc3def4gh5ijk6lmnop",
  "status": "success",
  "filesWritten": ["package.json", "src/App.jsx"],
  "install": {
    "success": false,
    "output": "npm ERR! ..."
  }
}
```

无活跃 Session (`404`):

```json
{
  "status": "error",
  "message": "No active session"
}
```

Session 状态不允许 (`409`):

```json
{
  "status": "error",
  "message": "Session is in 'creating' state, cannot sync files"
}
```

请求体无效 (`400`):

```json
{
  "status": "error",
  "message": "Missing or empty files object"
}
```

**注意事项**
- 仅当 Session 状态为 `running` 或 `timeout` 时允许推送文件
- 当 `files` 中包含 key 为 `package.json` 的项时，自动触发 `npm install --no-audit --no-fund`
- 依赖安装失败不会导致整个请求失败（文件已写入），通过 `install.success` 字段判断
- Host 模式下文件写入 `WORKSPACE_DIR` 目录；Docker 模式下写入容器 `/workspace` 目录

---

### DELETE /session

销毁当前 Session，停止 Vite 进程/容器。

**Response**

成功 (`200`):

```json
{
  "sessionId": "01j5x2abc3def4gh5ijk6lmnop",
  "status": "destroyed"
}
```

无活跃 Session (`404`):

```json
{
  "status": "error",
  "message": "No active session"
}
```

---

### WS /session/logs

通过 WebSocket 实时获取沙箱日志流（Vite 编译输出、npm install 日志等）。

**连接地址**

```
ws://localhost:4000/session/logs
```

**连接条件**
- 必须有活跃 Session
- Session 的 executor 必须处于运行状态
- 不满足条件时连接被直接断开

**消息格式（服务端 → 客户端）**

日志消息：

```json
{
  "type": "log",
  "stream": "stdout",
  "text": "  VITE v6.3.5  ready in 320 ms\n"
}
```

日志流结束：

```json
{
  "type": "end",
  "message": "Log stream ended"
}
```

错误：

```json
{
  "type": "error",
  "message": "..."
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"log" \| "end" \| "error"` | 消息类型 |
| `stream` | `"stdout" \| "stderr"` | 日志来源（仅 type=log） |
| `text` | string | 日志内容（仅 type=log） |
| `message` | string | 提示信息（仅 type=end/error） |

---

## 典型使用流程

```
1. POST /session                     → 创建沙箱，获取 previewUrl
2. PUT /session/files                → 推送 package.json + 代码文件
   ↳ 自动执行 npm install           → 安装新依赖
   ↳ Vite HMR 自动生效             → 预览立即更新
3. 浏览器打开 previewUrl             → 查看实时预览
4. WS /session/logs                  → 监听编译日志和错误
5. PUT /session/files                → 继续修改代码，重复推送
6. DELETE /session                   → 使用完毕，主动销毁
```

## 与 server2 的差异

| 维度 | server2 | server3 |
|------|---------|---------|
| Session | 多 Session + 端口池 | 单 Session 固定端口 |
| 路由 | `/sessions/:id/...` | `/session/...` |
| 隔离 | 仅 Docker | Docker / Host 可选 |
| 依赖安装 | 不支持 | 自动检测 package.json 并 npm install |
| 适用场景 | 多用户共享 | 单用户独占机器 |
