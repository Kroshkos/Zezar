const { ulid } = require("ulid");
const { createSession } = require("../domain/session");
const { createExecutor } = require("../infrastructure/executor");
const { CONTAINER_READY_TIMEOUT_MS, CONTAINER_READY_POLL_MS, PREVIEW_HOST_PORT } = require("../config");

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForReady(port, timeoutMs, pollMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/`, {
        signal: AbortSignal.timeout(1000),
      });
      if (res.ok || res.status === 404) return true;
    } catch {}
    await sleep(pollMs);
  }
  return false;
}

const SUPPORTED_TEMPLATES = ["react", "vue"];

async function handleCreateSession({ sessionRef, template }) {
  const tpl = template || "react";
  if (!SUPPORTED_TEMPLATES.includes(tpl)) {
    return {
      statusCode: 400,
      body: {
        status: "error",
        message: `Unsupported template "${tpl}". Supported: ${SUPPORTED_TEMPLATES.join(", ")}`,
      },
    };
  }

  if (sessionRef.current) {
    try {
      await sessionRef.current.executor.stop();
    } catch {}
    sessionRef.current = null;
  }

  const sessionId = ulid().toLowerCase();
  const executor = createExecutor(tpl);
  const session = createSession({ id: sessionId, executor, template: tpl });
  sessionRef.current = session;

  try {
    await executor.start();
    session.status = "starting";

    const ready = await waitForReady(
      PREVIEW_HOST_PORT,
      CONTAINER_READY_TIMEOUT_MS,
      CONTAINER_READY_POLL_MS
    );
    session.status = ready ? "running" : "timeout";

    return {
      statusCode: 201,
      body: {
        sessionId,
        template: tpl,
        status: session.status,
        previewUrl: `http://localhost:${PREVIEW_HOST_PORT}`,
      },
    };
  } catch (err) {
    session.status = "error";
    try { await executor.stop(); } catch {}
    sessionRef.current = null;
    return {
      statusCode: 500,
      body: { status: "error", message: err.message },
    };
  }
}

async function handleGetSession({ sessionRef }) {
  const session = sessionRef.current;
  if (!session) {
    return { statusCode: 404, body: { status: "error", message: "No active session" } };
  }
  return {
    statusCode: 200,
    body: {
      sessionId: session.id,
      status: session.status,
      previewUrl: `http://localhost:${PREVIEW_HOST_PORT}`,
      createdAt: session.createdAt,
    },
  };
}

async function handleDestroySession({ sessionRef }) {
  const session = sessionRef.current;
  if (!session) {
    return { statusCode: 404, body: { status: "error", message: "No active session" } };
  }

  try {
    await session.executor.stop();
  } catch (err) {
    console.error(`[session] failed to stop executor:`, err.message);
  }
  sessionRef.current = null;

  return {
    statusCode: 200,
    body: { sessionId: session.id, status: "destroyed" },
  };
}

module.exports = {
  handleCreateSession,
  handleGetSession,
  handleDestroySession,
};
