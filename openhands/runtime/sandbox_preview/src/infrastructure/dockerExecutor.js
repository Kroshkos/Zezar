const Docker = require("dockerode");
const tar = require("tar-stream");
const { EventEmitter } = require("events");
const {
  SANDBOX_IMAGE,
  SANDBOX_INTERNAL_PORT,
  PREVIEW_HOST_PORT,
  CONTAINER_MEMORY_LIMIT,
  CONTAINER_CPU_QUOTA,
  CONTAINER_CPU_PERIOD,
} = require("../config");

const docker = new Docker({
  socketPath: process.env.DOCKER_HOST
    ? undefined
    : process.env.DOCKER_SOCKET || "/var/run/docker.sock",
  ...(process.env.DOCKER_HOST ? { host: process.env.DOCKER_HOST } : {}),
});

const CONTAINER_NAME = "sandbox-session";

function createDockerExecutor(template = "react") {
  let containerId = null;
  let logStream = null;
  const logEmitter = new EventEmitter();
  let running = false;

  async function start() {
    await cleanup();

    const container = await docker.createContainer({
      Image: SANDBOX_IMAGE,
      name: CONTAINER_NAME,
      Labels: { "sandbox.managed": "true" },
      Env: [`TEMPLATE=${template}`],
      ExposedPorts: { [`${SANDBOX_INTERNAL_PORT}/tcp`]: {} },
      HostConfig: {
        PortBindings: {
          [`${SANDBOX_INTERNAL_PORT}/tcp`]: [{ HostPort: String(PREVIEW_HOST_PORT) }],
        },
        Memory: CONTAINER_MEMORY_LIMIT,
        CpuQuota: CONTAINER_CPU_QUOTA,
        CpuPeriod: CONTAINER_CPU_PERIOD,
        AutoRemove: false,
      },
    });

    await container.start();
    containerId = container.id;
    running = true;

    attachLogStream();
  }

  async function attachLogStream() {
    if (!containerId) return;
    const container = docker.getContainer(containerId);
    logStream = await container.logs({
      follow: true,
      stdout: true,
      stderr: true,
      timestamps: true,
      since: 0,
    });

    logStream.on("data", (chunk) => {
      const entries = demuxDockerStream(chunk);
      for (const entry of entries) {
        logEmitter.emit("log", entry);
      }
    });

    logStream.on("end", () => {
      logEmitter.emit("end", { code: 0 });
    });

    logStream.on("error", (err) => {
      logEmitter.emit("error", err);
    });
  }

  function demuxDockerStream(buffer) {
    const lines = [];
    let offset = 0;
    while (offset + 8 <= buffer.length) {
      const type = buffer[offset];
      const size = buffer.readUInt32BE(offset + 4);
      if (offset + 8 + size > buffer.length) break;
      const payload = buffer.slice(offset + 8, offset + 8 + size).toString("utf8");
      lines.push({
        stream: type === 2 ? "stderr" : "stdout",
        text: payload,
      });
      offset += 8 + size;
    }
    return lines;
  }

  async function stop() {
    if (logStream) {
      logStream.destroy();
      logStream = null;
    }
    if (containerId) {
      const container = docker.getContainer(containerId);
      try {
        await container.stop({ t: 5 });
      } catch (err) {
        if (err.statusCode !== 304 && err.statusCode !== 404) throw err;
      }
      try {
        await container.remove({ force: true });
      } catch (err) {
        if (err.statusCode !== 404) throw err;
      }
      containerId = null;
      running = false;
    }
  }

  async function cleanup() {
    const containers = await docker.listContainers({
      all: true,
      filters: { name: [CONTAINER_NAME] },
    });
    for (const c of containers) {
      const container = docker.getContainer(c.Id);
      try { await container.stop({ t: 2 }); } catch {}
      try { await container.remove({ force: true }); } catch {}
    }
  }

  const EXT_CONFLICTS = {
    ".jsx": [".tsx"],
    ".tsx": [".jsx"],
    ".js": [".ts", ".tsx"],
    ".ts": [".js", ".jsx"],
  };

  async function writeFiles(files) {
    if (!containerId) throw new Error("Container not running");
    const container = docker.getContainer(containerId);

    const rmTargets = [];
    for (const filePath of Object.keys(files)) {
      const ext = filePath.match(/\.[^.]+$/)?.[0];
      const conflicts = ext && EXT_CONFLICTS[ext];
      if (conflicts) {
        const base = filePath.slice(0, -ext.length);
        for (const conflictExt of conflicts) {
          rmTargets.push(`/workspace/${base}${conflictExt}`);
        }
      }
    }
    let hasConflict = false;
    if (rmTargets.length > 0) {
      try {
        await execCommand(`rm -f ${rmTargets.join(" ")}`);
        hasConflict = true;
      } catch {}
    }

    const pack = tar.pack();
    for (const [filePath, content] of Object.entries(files)) {
      pack.entry({ name: filePath }, content);
    }
    pack.finalize();

    await container.putArchive(pack, { path: "/workspace" });

    if (hasConflict) {
      try {
        await execCommand("touch /workspace/index.html");
      } catch {}
    }
  }

  async function execCommand(cmd) {
    if (!containerId) throw new Error("Container not running");
    const container = docker.getContainer(containerId);

    const execution = await container.exec({
      Cmd: ["sh", "-c", cmd],
      AttachStdout: true,
      AttachStderr: true,
      WorkingDir: "/workspace",
    });

    const stream = await execution.start({ hijack: true, stdin: false });

    return new Promise((resolve, reject) => {
      let stdout = "";
      let stderr = "";

      stream.on("data", (chunk) => {
        const entries = demuxDockerStream(chunk);
        for (const entry of entries) {
          if (entry.stream === "stderr") stderr += entry.text;
          else stdout += entry.text;
          logEmitter.emit("log", entry);
        }
      });

      stream.on("end", async () => {
        try {
          const inspect = await execution.inspect();
          if (inspect.ExitCode === 0) resolve({ stdout, stderr });
          else reject(new Error(`Command "${cmd}" exited with code ${inspect.ExitCode}\n${stderr}`));
        } catch (err) {
          reject(err);
        }
      });

      stream.on("error", reject);
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

module.exports = { createDockerExecutor };
