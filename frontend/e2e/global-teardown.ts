import { rmSync } from "node:fs";
import { basename } from "node:path";

export default function globalTeardown() {
  const directory = process.env.AI_TASK_ASSISTANT_E2E_TEMP_DIR;
  if (!directory || !basename(directory).startsWith("ai-task-assistant-e2e-")) return;
  try {
    rmSync(directory, { force: true, recursive: true });
    console.log("[e2e-server] temporary_resources_cleanup_complete");
  } catch {
    console.error("[e2e-server] temporary_resources_cleanup_failed");
    throw new Error("Unable to clean up E2E temporary resources.");
  }
}
