function attachLogs(ws, executor) {
  const logEmitter = executor.getLogEmitter();

  function onLog(entry) {
    if (ws.readyState !== 1) return;
    ws.send(JSON.stringify({ type: "log", stream: entry.stream, text: entry.text }));
  }

  function onEnd() {
    if (ws.readyState === 1) {
      ws.send(JSON.stringify({ type: "end", message: "Log stream ended" }));
      ws.close();
    }
  }

  function onError(err) {
    if (ws.readyState === 1) {
      ws.send(JSON.stringify({ type: "error", message: err.message }));
      ws.close();
    }
  }

  logEmitter.on("log", onLog);
  logEmitter.on("end", onEnd);
  logEmitter.on("error", onError);

  ws.on("close", () => {
    logEmitter.off("log", onLog);
    logEmitter.off("end", onEnd);
    logEmitter.off("error", onError);
  });
}

module.exports = { attachLogs };
