import { defineConfig } from "@playwright/test";
import { existsSync, mkdtempSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontendDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(frontendDirectory, "..");
const temporaryDirectory = mkdtempSync(join(tmpdir(), "ai-task-assistant-e2e-"));
const databasePath = join(temporaryDirectory, "browser-e2e.sqlite3");
const localPython = join(repositoryRoot, ".venv", "bin", "python");

process.env.AI_TASK_ASSISTANT_E2E_TEMP_DIR = temporaryDirectory;

export default defineConfig({
  testDir: "./e2e",
  globalTeardown: "./e2e/global-teardown.ts",
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "node e2e/serve.mjs",
    cwd: frontendDirectory,
    env: {
      AI_TASK_ASSISTANT_E2E_TEMP_DIR: temporaryDirectory,
      AI_TASK_ASSISTANT_E2E_DATABASE_PATH: databasePath,
      AI_TASK_ASSISTANT_E2E_INSTANCE_ID: randomUUID(),
      E2E_PYTHON_COMMAND: existsSync(localPython) ? localPython : "python",
    },
    url: "http://127.0.0.1:5173",
    // A port conflict is a test-environment failure, never permission to use
    // an arbitrary existing development server.
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
