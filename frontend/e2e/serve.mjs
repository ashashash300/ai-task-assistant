import { createServer } from "node:net";
import { spawn } from "node:child_process";
import { accessSync, constants } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontendDirectory = dirname(dirname(fileURLToPath(import.meta.url)));
const repositoryRoot = resolve(frontendDirectory, "..");
const backendPort = 8001;
const frontendPort = 5173;
const instanceId = process.env.AI_TASK_ASSISTANT_E2E_INSTANCE_ID;
const databasePath = process.env.AI_TASK_ASSISTANT_E2E_DATABASE_PATH;
const pythonCommand = process.env.E2E_PYTHON_COMMAND ?? "python";

if (!instanceId || !databasePath || process.env.AI_TASK_ASSISTANT_E2E_TEMP_DIR == null) {
  throw new Error("The E2E server requires an explicit isolated test configuration.");
}

function assertPortAvailable(port) {
  return new Promise((resolvePort, rejectPort) => {
    const probe = createServer();
    probe.once("error", () => {
      rejectPort(new Error(`E2E port ${port} is already in use; refusing to use an existing server.`));
    });
    probe.once("listening", () => {
      probe.close((error) => (error ? rejectPort(error) : resolvePort()));
    });
    probe.listen(port, "127.0.0.1");
  });
}

function sleep(milliseconds) {
  return new Promise((resolveSleep) => setTimeout(resolveSleep, milliseconds));
}

async function waitFor(url, label) {
  const deadline = Date.now() + 25_000;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
      lastError = new Error(`${label} responded with ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await sleep(200);
  }
  throw new Error(`${label} did not start: ${lastError instanceof Error ? lastError.message : "unknown error"}`);
}

function spawnOwned(command, args, options) {
  const child = spawn(command, args, {
    ...options,
    stdio: "inherit",
  });
  child.once("error", (error) => {
    // Keep launcher output free of command-line arguments and environment
    // values, which can contain test-only credentials.
    console.error("[e2e-server] child_start_failed");
  });
  return child;
}

let backend;
let frontend;
let stopping = false;

function stopOwnedChildren(exitCode = 0) {
  if (stopping) return;
  stopping = true;
  console.log("[e2e-server] cleanup_requested");
  for (const child of [frontend, backend]) {
    if (child?.exitCode === null && !child.killed) child.kill("SIGTERM");
  }
  setTimeout(() => {
    for (const child of [frontend, backend]) {
      if (child?.exitCode === null) child.kill("SIGKILL");
    }
    process.exit(exitCode);
  }, 2_000).unref();
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.once(signal, () => stopOwnedChildren(0));
}

try {
  accessSync(pythonCommand, constants.X_OK);
} catch {
  // A PATH command such as "python" is resolved by spawn. Absolute local
  // virtualenv paths were already checked by the Playwright configuration.
}

try {
  await assertPortAvailable(backendPort);
  await assertPortAvailable(frontendPort);

  const backendEnvironment = {
    ...process.env,
    AI_TASK_ASSISTANT_DISABLE_DOTENV: "1",
    AI_TASK_ASSISTANT_E2E: "1",
    AI_TASK_ASSISTANT_E2E_INSTANCE_ID: instanceId,
    DATABASE_URL: `sqlite:///${databasePath}`,
    AI_PROVIDER: "openai",
    OPENAI_API_KEY: "browser-e2e-placeholder",
    GEMINI_API_KEY: "browser-e2e-placeholder",
    SESSION_COOKIE_SECURE: "false",
    AUTH_ALLOWED_ORIGINS: `http://127.0.0.1:${frontendPort}`,
    CHAT_DAILY_QUOTA: "1",
    PYTHONPATH: "src",
  };
  backend = spawnOwned(pythonCommand, ["scripts/browser_e2e_server.py"], {
    cwd: repositoryRoot,
    env: backendEnvironment,
  });
  backend.once("exit", (code) => {
    if (!stopping) {
      console.error(`[e2e-server] backend_unexpected_exit code=${code ?? "signal"}`);
      stopOwnedChildren(1);
    } else {
      console.log(`[e2e-server] backend_stopped code=${code ?? "signal"}`);
    }
  });
  await waitFor(`http://127.0.0.1:${backendPort}/health`, "E2E backend");
  console.log("[e2e-server] backend_ready");

  frontend = spawnOwned(process.execPath, [
    resolve(frontendDirectory, "node_modules", "vite", "bin", "vite.js"),
    "--host",
    "127.0.0.1",
    "--port",
    String(frontendPort),
  ], {
    cwd: frontendDirectory,
    env: {
      ...process.env,
      VITE_API_PROXY_TARGET: `http://127.0.0.1:${backendPort}`,
    },
  });
  frontend.once("exit", (code) => {
    if (!stopping) {
      console.error(`[e2e-server] frontend_unexpected_exit code=${code ?? "signal"}`);
      stopOwnedChildren(1);
    } else {
      console.log(`[e2e-server] frontend_stopped code=${code ?? "signal"}`);
    }
  });
  await waitFor(`http://127.0.0.1:${frontendPort}`, "E2E frontend");
  console.log("[e2e-server] frontend_ready");
} catch (error) {
  // Startup errors sometimes contain a full URL. The phase diagnostics do not
  // need it, so retain only a stable failure category in CI output.
  console.error("[e2e-server] startup_failed");
  stopOwnedChildren(1);
}

await new Promise(() => {});
