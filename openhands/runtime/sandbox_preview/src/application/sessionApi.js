const { ulid } = require("ulid");
const { createSession } = require("../domain/session");
const { createExecutor } = require("../infrastructure/executor");
const { CONTAINER_READY_TIMEOUT_MS, CONTAINER_READY_POLL_MS, PREVIEW_HOST_PORT } = require("../config");

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForReady(port, timeoutMs, pollMs) {
  const deadline = Date.now() + timeoutMs;
  let attempts = 0;
  while (Date.now() < deadline) {
    attempts++;
    try {
      const res = await fetch(`http://127.0.0.1:${port}/`, {
        signal: AbortSignal.timeout(1000),
      });
      if (res.ok || res.status === 404) {
        console.log(`[waitForReady] ready after ${attempts} attempts, status=${res.status}`);
        return true;
      }
    } catch (err) {
      // expected during startup
    }
    await sleep(pollMs);
  }
  console.warn(`[waitForReady] timeout after ${attempts} attempts (${timeoutMs}ms) on port ${port}`);
  return false;
}

const SUPPORTED_TEMPLATES = ["react", "vue"];

async function handleCreateSession({ sessionRef, template }) {
  const tpl = template || "react";
  console.log(`[handleCreateSession] start template=${tpl}`);
  if (!SUPPORTED_TEMPLATES.includes(tpl)) {
    console.warn(`[handleCreateSession] unsupported template: ${tpl}`);
    return {
      statusCode: 400,
      body: {
        status: "error",
        message: `Unsupported template "${tpl}". Supported: ${SUPPORTED_TEMPLATES.join(", ")}`,
      },
    };
  }

  if (sessionRef.current) {
    console.log(`[handleCreateSession] stopping previous session id=${sessionRef.current.id}`);
    try {
      await sessionRef.current.executor.stop();
    } catch (err) {
      console.warn(`[handleCreateSession] stop previous executor failed: ${err.message}`);
    }
    sessionRef.current = null;
  }

  const sessionId = ulid().toLowerCase();
  const executor = createExecutor(tpl);
  const session = createSession({ id: sessionId, executor, template: tpl });
  sessionRef.current = session;
  console.log(`[handleCreateSession] created session id=${sessionId} template=${tpl}`);

  try {
    const tStart = Date.now();
    await executor.start();
    console.log(`[handleCreateSession] executor.start done cost=${Date.now() - tStart}ms`);
    session.status = "starting";

    const tReady = Date.now();
    const ready = await waitForReady(
      PREVIEW_HOST_PORT,
      CONTAINER_READY_TIMEOUT_MS,
      CONTAINER_READY_POLL_MS
    );
    console.log(`[handleCreateSession] waitForReady=${ready} cost=${Date.now() - tReady}ms`);
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
    console.error(`[handleCreateSession] error: ${err.message}`, err.stack);
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
    console.log(`[handleGetSession] no active session`);
    return { statusCode: 404, body: { status: "error", message: "No active session" } };
  }
  console.log(`[handleGetSession] id=${session.id} status=${session.status}`);
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
    console.log(`[handleDestroySession] no active session`);
    return { statusCode: 404, body: { status: "error", message: "No active session" } };
  }

  console.log(`[handleDestroySession] stopping session id=${session.id}`);
  try {
    await session.executor.stop();
    console.log(`[handleDestroySession] executor stopped id=${session.id}`);
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
