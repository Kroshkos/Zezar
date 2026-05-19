# Sandbox Preview Service

单用户沙箱预览服务，基于 Node.js/Express + Vite，为 React 代码提供实时预览能力。

## 架构

```
Action Execution Server (Python, :35123)
       │
       │  HTTP Proxy (/sandbox/* → localhost:4000)
       ▼
┌─────────────────────────────────┐
│   Sandbox Preview Service       │
│   (Node.js/Express)             │
│   Port: 4000 (API)              │
│                                 │
│   ┌───────────────────────────┐ │
│   │   Executor (Host/Docker)  │ │
│   │   - initTemplate          │ │
│   │   - installDeps           │ │
│   │   - writeFiles            │ │
│   │   - start/stop Vite       │ │
│   └───────────────────────────┘ │
│               │                 │
│               ▼                 │
│   ┌───────────────────────────┐ │
│   │   Vite Dev Server         │ │
│   │   Port: 4001 (Preview)    │ │
│   │   - React HMR             │ │
│   │   - JSX 编译               │ │
│   └───────────────────────────┘ │
└─────────────────────────────────┘
```

## 服务端口


| 服务          | 端口        | 说明                            |
| ----------- | --------- | ----------------------------- |
| Python API  | **35123** | Action Execution Server（代理入口） |
| Sandbox API | **4000**  | Node.js 沙箱服务                  |
| Preview     | **4001**  | Vite 预览页面                     |


## 快速启动

### 统一启动（推荐）

使用项目根目录的统一脚本同时启动 Python + Node.js 服务：

```bash
./start_services.sh
```

### 单独启动 Sandbox 服务

#### Host 模式（推荐本地开发）

```bash
cd openhands/runtime/sandbox_preview
npm install

SANDBOX_MODE=host WORKSPACE_DIR=/workspace PREVIEW_HOST_PORT=4001 node server.js
```

#### Docker 模式

```bash
cd openhands/runtime/sandbox_preview
npm install
npm run build-sandbox   # 构建沙箱镜像（首次需要）

SANDBOX_MODE=docker node server.js
```

> Docker 模式需要本机安装 Docker 并确保 docker.sock 可访问。

## 运行模式

通过环境变量 `SANDBOX_MODE` 切换：


| 模式           | 说明                         |
| ------------ | -------------------------- |
| `host`       | 直接在宿主机运行 Vite 子进程，性能好、部署简单 |
| `docker`（默认） | 在 Docker 容器中运行 Vite，隔离性好   |


## 环境变量


| 变量                  | 默认值            | 说明                     |
| ------------------- | -------------- | ---------------------- |
| `PORT`              | `4000`         | API 服务端口               |
| `SANDBOX_MODE`      | `docker`       | 运行模式：`docker` 或 `host` |
| `PREVIEW_HOST_PORT` | `4001`         | 预览页面端口                 |
| `WORKSPACE_DIR`     | `/workspace`   | Host 模式下的工作目录          |
| `SANDBOX_IMAGE`     | `sandbox-vite` | Docker 模式的镜像名          |


## 代理接口

通过 Python 服务（端口 35123）的 `/sandbox/*` 统一代理访问沙箱服务：

```
Python API (35123)                →  Sandbox Service (4000)
─────────────────────────────────────────────────────────────
GET  /sandbox_preview_url        →  返回沙箱服务 URL 信息
GET  /sandbox/health             →  GET  /health
POST /sandbox/session            →  POST /session（创建沙箱）
GET  /sandbox/session            →  GET  /session（获取状态）
PUT  /sandbox/session/files      →  PUT  /session/files（推送代码）
DELETE /sandbox/session          →  DELETE /session（销毁沙箱）
WS   /sandbox/session/logs       →  WS   /session/logs（实时日志）
```

## 使用示例

```bash
# 1. 创建 Session
curl -X POST http://localhost:35123/sandbox/session
# 返回: {"sessionId":"xxx","status":"running","previewUrl":"http://localhost:4001"}

# 2. 推送代码（自动安装依赖）
curl -X PUT http://localhost:35123/sandbox/session/files \
  -H "Content-Type: application/json" \
  -d '{
    "files": {
      "package.json": "{\"name\":\"my-app\",\"private\":true,\"type\":\"module\",\"dependencies\":{\"react\":\"^18.2.0\",\"react-dom\":\"^18.2.0\",\"lodash\":\"^4.17.21\"},\"devDependencies\":{\"@vitejs/plugin-react\":\"^4.3.4\",\"vite\":\"^6.3.5\"}}",
      "src/App.jsx": "import _ from \"lodash\";\nexport default function App() {\n  return <h1>{_.camelCase(\"09090hello world\")}</h1>;\n}"
    }
  }'

# 3. 浏览器打开预览
open http://localhost:4001

# 4. 查看 Session 状态
curl http://localhost:35123/sandbox/session

# 5. 销毁 Session
curl -X DELETE http://localhost:35123/sandbox/session
```

## 实时日志

通过 WebSocket 监听编译日志：

```bash
npx wscat -c ws://localhost:4000/session/logs
```

消息格式：

```json
{"type":"log","stream":"stdout","text":"VITE v6.3.5 ready in 320 ms\n"}
{"type":"end","message":"Log stream ended"}
{"type":"error","message":"..."}
```

## 接口文档

详见 [API.md](./API.md)

---

## 测试用例

### 1. Session 生命周期

```bash
# 1.1 创建 Session
curl -X POST http://localhost:35123/sandbox/session
# 预期: {"sessionId":"...","status":"running","previewUrl":"http://localhost:4001"}

# 1.2 获取 Session 状态
curl http://localhost:35123/sandbox/session
# 预期: {"sessionId":"...","status":"running","previewUrl":"...","createdAt":...}

# 1.3 获取预览 URL 信息
curl http://localhost:35123/sandbox_preview_url
# 预期: {"api_url":"http://localhost:4000","preview_url":"http://localhost:4001"}

# 1.4 销毁 Session
curl -X DELETE http://localhost:35123/sandbox/session
# 预期: {"sessionId":"...","status":"destroyed"}

# 1.5 销毁后再次获取 Session（应 404）
curl http://localhost:35123/sandbox/session
# 预期: {"status":"error","message":"No active session"}

# 1.6 重复创建 Session（旧的自动销毁）
curl -X POST http://localhost:35123/sandbox/session
curl -X POST http://localhost:35123/sandbox/session
# 预期: 第二次创建成功，返回新的 sessionId
```

### 2. 代码推送与预览

```bash
# 2.1 推送简单 React 组件（不含 package.json，不触发 npm install）
curl -X PUT http://localhost:35123/sandbox/session/files \
  -H "Content-Type: application/json" \
  -d '{"files":{"src/App.jsx":"export default function App() { return <h1>Hello World</h1>; }"}}'
# 预期: {"sessionId":"...","status":"success","filesWritten":["src/App.jsx"],"install":null}

# 2.2 推送含 package.json 的文件（触发自动 npm install）
curl -X PUT http://localhost:35123/sandbox/session/files \
  -H "Content-Type: application/json" \
  -d '{"files":{"package.json":"{\"name\":\"my-app\",\"private\":true,\"type\":\"module\",\"dependencies\":{\"react\":\"^18.2.0\",\"react-dom\":\"^18.2.0\",\"lodash\":\"^4.17.21\"},\"devDependencies\":{\"@vitejs/plugin-react\":\"^4.3.4\",\"vite\":\"^6.3.5\"}}","src/App.jsx":"import _ from \"lodash\";\nexport default function App() { return <h1>{_.camelCase(\"hello world\")}</h1>; }"}}'
# 预期: {"sessionId":"...","status":"success","filesWritten":["package.json","src/App.jsx"],"install":{"success":true,"output":"..."}}

# 2.3 推送后验证预览页面可访问
curl -s http://localhost:4001/ | grep "<div id=\"root\">"
# 预期: 输出包含 <div id="root"></div>

# 2.4 验证 Vite 正确编译 JSX
curl -s http://localhost:4001/src/App.jsx | head -3
# 预期: 返回经 Vite 编译后的 JS 代码（含 import 语句）

# 2.5 推送修改后的代码，验证 HMR 生效
curl -X PUT http://localhost:35123/sandbox/session/files \
  -H "Content-Type: application/json" \
  -d '{"files":{"src/App.jsx":"export default function App() { return <h1>Updated!</h1>; }"}}'
# 预期: 浏览器自动热更新显示 "Updated!"
```

### 3. 代码推送异常场景

```bash
# 3.1 无 Session 时推送文件（应 404）
curl -X DELETE http://localhost:35123/sandbox/session
curl -X PUT http://localhost:35123/sandbox/session/files \
  -H "Content-Type: application/json" \
  -d '{"files":{"src/App.jsx":"export default function App() { return <h1>Test</h1>; }"}}'
# 预期: {"status":"error","message":"No active session"}

# 3.2 推送空 files 对象（应 400）
curl -X POST http://localhost:35123/sandbox/session
curl -X PUT http://localhost:35123/sandbox/session/files \
  -H "Content-Type: application/json" \
  -d '{"files":{}}'
# 预期: {"status":"error","message":"Missing or empty files object"}

# 3.3 推送无效的 package.json（npm install 失败，文件仍写入）
curl -X PUT http://localhost:35123/sandbox/session/files \
  -H "Content-Type: application/json" \
  -d '{"files":{"package.json":"{\"dependencies\":{\"this-package-does-not-exist-xyz\":\"^99.99.99\"}}","src/App.jsx":"export default function App() { return <h1>Test</h1>; }"}}'
# 预期: {"sessionId":"...","status":"success","filesWritten":[...],"install":{"success":false,"output":"npm ERR! ..."}}

# 3.4 推送非 JSON body（应报错）
curl -X PUT http://localhost:35123/sandbox/session/files \
  -H "Content-Type: application/json" \
  -d 'invalid json'
# 预期: HTTP 400 或 422
```

### 4. WebSocket 日志流

```bash
# 4.1 连接日志 WebSocket（需先创建 Session）
curl -X POST http://localhost:4000/session
npx wscat -c ws://localhost:4000/session/logs
# 预期: 收到 {"type":"log","stream":"stdout","text":"..."} 格式的 Vite 日志

# 4.2 推送代码后观察 HMR 日志
# 在 WebSocket 连接中推送文件，观察 Vite 热更新日志输出
# 预期: 收到 hmr update 相关日志

# 4.3 无 Session 时连接 WebSocket（应断开）
curl -X DELETE http://localhost:4000/session
npx wscat -c ws://localhost:4000/session/logs
# 预期: 连接被服务端直接断开
```

### 5. 健康检查

```bash
# 5.1 Sandbox 服务健康检查
curl http://localhost:4000/health
# 预期: OK

# 5.2 通过代理访问健康检查
curl http://localhost:35123/sandbox/health
# 预期: OK

# 5.3 Sandbox 服务首页
curl http://localhost:4000/
# 预期: 返回 HTML 页面，包含服务说明
```

### 6. 端到端完整流程

```bash
# 完整的开发预览流程测试

# Step 1: 启动服务
./start_services.sh

# Step 2: 创建 Session
curl -X POST http://localhost:35123/sandbox/session

# Step 3: 推送完整的 React 应用代码
curl -X PUT http://localhost:35123/sandbox/session/files \
  -H "Content-Type: application/json" \
  -d '{
    "files": {
      "package.json": "{\"name\":\"demo\",\"private\":true,\"type\":\"module\",\"dependencies\":{\"react\":\"^18.2.0\",\"react-dom\":\"^18.2.0\"},\"devDependencies\":{\"@vitejs/plugin-react\":\"^4.3.4\",\"vite\":\"^6.3.5\"}}",
      "src/App.jsx": "import { useState } from \"react\";\nexport default function App() {\n  const [count, setCount] = useState(0);\n  return (\n    <div style={{padding:24}}>\n      <h1>Counter: {count}</h1>\n      <button onClick={() => setCount(c => c+1)}>+1</button>\n    </div>\n  );\n}",
      "src/main.jsx": "import React from \"react\";\nimport ReactDOM from \"react-dom/client\";\nimport App from \"./App.jsx\";\nReactDOM.createRoot(document.getElementById(\"root\")).render(<App />);",
      "index.html": "<!DOCTYPE html>\n<html><head><meta charset=\"UTF-8\"/></head><body><div id=\"root\"></div><script type=\"module\" src=\"/src/main.jsx\"></script></body></html>",
      "vite.config.js": "import { defineConfig } from \"vite\";\nimport react from \"@vitejs/plugin-react\";\nexport default defineConfig({ plugins: [react()], server: { host: \"0.0.0.0\", port: 3000, strictPort: true, hmr: { port: 3000 } } });"
    }
  }'
# 预期: install.success=true

# Step 4: 浏览器打开 http://localhost:4001 验证页面渲染
# 预期: 显示 Counter 组件，按钮可点击

# Step 5: 修改代码后推送，验证热更新
curl -X PUT http://localhost:35123/sandbox/session/files \
  -H "Content-Type: application/json" \
  -d '{"files":{"src/App.jsx":"export default function App() { return <h1>Updated!</h1>; }"}}'
# 预期: 浏览器自动更新显示 "Updated!"

# Step 6: 清理
curl -X DELETE http://localhost:35123/sandbox/session
```

