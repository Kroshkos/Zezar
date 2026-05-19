async function handleFileSync({ sessionRef, files }) {
  const session = sessionRef.current;
  if (!session) {
    return { statusCode: 404, body: { status: "error", message: "No active session" } };
  }

  if (session.status !== "running" && session.status !== "timeout") {
    return {
      statusCode: 409,
      body: { status: "error", message: `Session is in '${session.status}' state, cannot sync files` },
    };
  }

  if (!files || typeof files !== "object" || Object.keys(files).length === 0) {
    return { statusCode: 400, body: { status: "error", message: "Missing or empty files object" } };
  }

  const { executor } = session;

  try {
    await executor.writeFiles(files);

    let installResult = null;
    if (files["package.json"]) {
      try {
        const result = await executor.exec("npm install --no-audit --no-fund");
        installResult = { success: true, output: result.stdout || result.stderr };
      } catch (err) {
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
    return {
      statusCode: 500,
      body: { status: "error", message: err.message },
    };
  }
}

module.exports = { handleFileSync };
