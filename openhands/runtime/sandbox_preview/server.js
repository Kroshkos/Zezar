const express = require("express");
const { createServer } = require("http");
const cors = require("cors");
const { WebSocketServer } = require("ws");
const { PORT, SANDBOX_MODE, PREVIEW_HOST_PORT, JSON_BODY_LIMIT } = require("./src/config");
const { attachLogs } = require("./src/infrastructure/logStreamer");
const {
  handleCreateSession,
  handleGetSession,
  handleDestroySession,
} = require("./src/application/sessionApi");
const { handleFileSync } = require("./src/application/fileSyncApi");

const app = express();
const httpServer = createServer(app);

app.use(cors());
app.use(express.json({ limit: JSON_BODY_LIMIT }));

const sessionRef = { current: null };

app.get("/", (_, res) => {
  res.type("html").send(`<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>Sandbox Service</title></head>
<body style="font-family:system-ui;max-width:640px;margin:40px auto;padding:0 16px;line-height:1.5;color:#334155">
  <h1 style="font-size:1.25rem">Sandbox Orchestration Service (v3)</h1>
  <p>Single-user preview with auto dependency install. Mode: <strong>${SANDBOX_MODE}</strong></p>
  <ul>
    <li><a href="/health">GET /health</a></li>
    <li><code>POST /session</code> — Create/rebuild sandbox session</li>
    <li><code>GET /session</code> — Get current session status</li>
    <li><code>PUT /session/files</code> — Push code files (auto npm install)</li>
    <li><code>DELETE /session</code> — Destroy session</li>
    <li><code>WS /session/logs</code> — Real-time log stream</li>
  </ul>
  <p style="font-size:0.875rem;color:#64748b">API Port: ${PORT} | Preview Port: ${PREVIEW_HOST_PORT}</p>
</body></html>`);
});

app.get("/health", (_, res) => res.send("OK"));

app.post("/session", async (req, res) => {
  console.log("[POST /session] req.body:", JSON.stringify(req.body));
  const t0 = Date.now();
  const result = await handleCreateSession({
    sessionRef,
    template: req.body?.template,
  });
  console.log(`[POST /session] result status=${result.statusCode} cost=${Date.now() - t0}ms body=${JSON.stringify(result.body)}`);
  res.status(result.statusCode).json(result.body);
});

app.get("/session", async (_, res) => {
  console.log("[GET /session] called");
  const result = await handleGetSession({ sessionRef });
  console.log(`[GET /session] result status=${result.statusCode} body=${JSON.stringify(result.body)}`);
  res.status(result.statusCode).json(result.body);
});

app.delete("/session", async (_, res) => {
  console.log("[DELETE /session] called");
  const result = await handleDestroySession({ sessionRef });
  console.log(`[DELETE /session] result status=${result.statusCode} body=${JSON.stringify(result.body)}`);
  res.status(result.statusCode).json(result.body);
});

app.put("/session/files", async (req, res) => {
  const fileKeys = req.body?.files ? Object.keys(req.body.files) : [];
  console.log(`[PUT /session/files] file count=${fileKeys.length} keys=${JSON.stringify(fileKeys)}`);
  const t0 = Date.now();
  const result = await handleFileSync({
    sessionRef,
    files: req.body.files,
  });
  console.log(`[PUT /session/files] result status=${result.statusCode} cost=${Date.now() - t0}ms body=${JSON.stringify(result.body)}`);
  res.status(result.statusCode).json(result.body);
});

const wss = new WebSocketServer({ noServer: true });

httpServer.on("upgrade", (req, socket, head) => {
  if (req.url !== "/session/logs") {
    socket.destroy();
    return;
  }

  const session = sessionRef.current;
  if (!session || !session.executor || !session.executor.isRunning()) {
    socket.destroy();
    return;
  }

  wss.handleUpgrade(req, socket, head, (ws) => {
    attachLogs(ws, session.executor);
  });
});

async function gracefulShutdown(signal) {
  console.log(`\n[server] ${signal} received, cleaning up...`);
  if (sessionRef.current) {
    try {
      await sessionRef.current.executor.stop();
    } catch (err) {
      console.error("[server] Cleanup error:", err.message);
    }
  }
  process.exit(0);
}

process.on("SIGTERM", () => gracefulShutdown("SIGTERM"));
process.on("SIGINT", () => gracefulShutdown("SIGINT"));

httpServer.listen(PORT, () => {
  console.log(`[server3] Mode: ${SANDBOX_MODE}`);
  console.log(`[server3] API ready on http://localhost:${PORT}`);
  console.log(`[server3] Preview will be at http://localhost:${PREVIEW_HOST_PORT}`);
});
