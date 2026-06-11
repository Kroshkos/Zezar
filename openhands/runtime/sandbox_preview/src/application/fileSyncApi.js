async function handleFileSync({ sessionRef, files }) {
  const session = sessionRef.current;
  if (!session) {
    console.log(`[handleFileSync] no active session`);
    return { statusCode: 404, body: { status: "error", message: "No active session" } };
  }

  if (session.status !== "running" && session.status !== "timeout") {
    console.warn(`[handleFileSync] invalid session status=${session.status}`);
    return {
      statusCode: 409,
      body: { status: "error", message: `Session is in '${session.status}' state, cannot sync files` },
    };
  }

  if (!files || typeof files !== "object" || Object.keys(files).length === 0) {
    console.warn(`[handleFileSync] missing or empty files object`);
    return { statusCode: 400, body: { status: "error", message: "Missing or empty files object" } };
  }

  const fileKeys = Object.keys(files);
  console.log(`[handleFileSync] start sessionId=${session.id} files=${fileKeys.length} keys=${JSON.stringify(fileKeys)}`);

  const { executor } = session;

  try {
    const tWrite = Date.now();
    await executor.writeFiles(files);
    console.log(`[handleFileSync] writeFiles done cost=${Date.now() - tWrite}ms`);

    let installResult = null;
    if (files["package.json"]) {
      console.log(`[handleFileSync] package.json detected, running npm install`);
      const tInstall = Date.now();
      try {
        const result = await executor.exec("npm install --no-audit --no-fund");
        console.log(`[handleFileSync] npm install success cost=${Date.now() - tInstall}ms`);
        installResult = { success: true, output: result.stdout || result.stderr };
      } catch (err) {
        console.error(`[handleFileSync] npm install failed cost=${Date.now() - tInstall}ms err=${err.message}`);
        installResult = { success: false, output: err.message };
      }
    }

    return {
      statusCode: 200,
      body: {
        sessionId: session.id,
        status: "success",
        filesWritten: Object.keys(files),
        install: installResult,
      },
    };
  } catch (err) {
    console.error(`[handleFileSync] error: ${err.message}`, err.stack);
    return {
      statusCode: 500,
      body: { status: "error", message: err.message },
    };
  }
}

module.exports = { handleFileSync };
