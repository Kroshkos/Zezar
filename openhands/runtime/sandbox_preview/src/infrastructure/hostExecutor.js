const { spawn, exec } = require("child_process");
const fs = require("fs/promises");
const path = require("path");
const { EventEmitter } = require("events");
const treeKill = require("tree-kill");
const { WORKSPACE_DIR, PREVIEW_HOST_PORT } = require("../config");

function createHostExecutor(template = "react") {
  let viteProcess = null;
  const logEmitter = new EventEmitter();
  let running = false;

  async function resetWorkspace() {
    await fs.rm(WORKSPACE_DIR, { recursive: true, force: true });
    await fs.mkdir(WORKSPACE_DIR, { recursive: true });
  }

  async function initTemplate() {
    const templateDir = path.join(__dirname, `../../sandbox-image/templates/${template}`);
    const SKIP = new Set(["node_modules", "pnpm-lock.yaml", ".git"]);
    const entries = await fs.readdir(templateDir, { withFileTypes: true });
    for (const entry of entries) {
      if (SKIP.has(entry.name)) continue;
      const src = path.join(templateDir, entry.name);
      const dest = path.join(WORKSPACE_DIR, entry.name);
      if (entry.isDirectory()) {
        await fs.cp(src, dest, { recursive: true });
      } else {
        await fs.copyFile(src, dest);
      }
    }
  }

  async function installDeps() {
    return new Promise((resolve, reject) => {
      const child = exec("npm install --no-audit --no-fund", {
        cwd: WORKSPACE_DIR,
        timeout: 120000,
      });

      let stdout = "";
      let stderr = "";

      child.stdout.on("data", (data) => {
        stdout += data;
        logEmitter.emit("log", { stream: "stdout", text: data.toString() });
      });
      child.stderr.on("data", (data) => {
        stderr += data;
        logEmitter.emit("log", { stream: "stderr", text: data.toString() });
      });
      child.on("close", (code) => {
        if (code === 0) resolve({ stdout, stderr });
        else reject(new Error(`npm install exited with code ${code}\n${stderr}`));
      });
      child.on("error", reject);
    });
  }

  async function start() {
    await resetWorkspace();
    await initTemplate();
    await installDeps();

    viteProcess = spawn("npx", ["vite", "--host", "0.0.0.0", "--port", String(PREVIEW_HOST_PORT)], {
      cwd: WORKSPACE_DIR,
      stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env, FORCE_COLOR: "1" },
    });

    viteProcess.stdout.on("data", (data) => {
      logEmitter.emit("log", { stream: "stdout", text: data.toString() });
    });
    viteProcess.stderr.on("data", (data) => {
      logEmitter.emit("log", { stream: "stderr", text: data.toString() });
    });
    viteProcess.on("close", (code) => {
      running = false;
      logEmitter.emit("end", { code });
    });

    running = true;
  }

  async function stop() {
    if (viteProcess && running) {
      await new Promise((resolve) => {
        treeKill(viteProcess.pid, "SIGTERM", () => resolve());
      });
      running = false;
      viteProcess = null;
    }
  }

  const EXT_CONFLICTS = {
    ".jsx": [".tsx"],
    ".tsx": [".jsx"],
    ".js": [".ts", ".tsx"],
    ".ts": [".js", ".jsx"],
  };

  async function writeFiles(files) {
    let hasConflict = false;
    for (const filePath of Object.keys(files)) {
      const fullPath = path.join(WORKSPACE_DIR, filePath);
      const ext = path.extname(filePath);
      const conflicts = EXT_CONFLICTS[ext];
      if (conflicts) {
        const base = fullPath.slice(0, -ext.length);
        for (const conflictExt of conflicts) {
          try {
            await fs.access(base + conflictExt);
            await fs.rm(base + conflictExt, { force: true });
            hasConflict = true;
          } catch {}
        }
      }
    }

    for (const [filePath, content] of Object.entries(files)) {
      const fullPath = path.join(WORKSPACE_DIR, filePath);
      await fs.mkdir(path.dirname(fullPath), { recursive: true });
      await fs.writeFile(fullPath, content, "utf-8");
    }

    if (hasConflict) {
      const indexHtml = path.join(WORKSPACE_DIR, "index.html");
      const now = new Date();
      await fs.utimes(indexHtml, now, now).catch(() => {});
    }
  }

  async function execCommand(cmd) {
    return new Promise((resolve, reject) => {
      const child = exec(cmd, {
        cwd: WORKSPACE_DIR,
        timeout: 120000,
      });

      let stdout = "";
      let stderr = "";

      child.stdout.on("data", (data) => {
        stdout += data;
        logEmitter.emit("log", { stream: "stdout", text: data.toString() });
      });
      child.stderr.on("data", (data) => {
        stderr += data;
        logEmitter.emit("log", { stream: "stderr", text: data.toString() });
      });
      child.on("close", (code) => {
        if (code === 0) resolve({ stdout, stderr });
        else reject(new Error(`Command "${cmd}" exited with code ${code}\n${stderr}`));
      });
      child.on("error", reject);
    });
  }

  function getLogEmitter() {
    return logEmitter;
  }

  function isRunning() {
    return running;
  }

  return { start, stop, writeFiles, exec: execCommand, getLogEmitter, isRunning };
}

module.exports = { createHostExecutor };
