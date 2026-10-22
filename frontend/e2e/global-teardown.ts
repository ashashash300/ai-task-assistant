import { rmSync } from "node:fs";
import { basename } from "node:path";

export default function globalTeardown() {
  const directory = process.env.AI_TASK_ASSISTANT_E2E_TEMP_DIR;
  if (!directory || !basename(directory).startsWith("ai-task-assistant-e2e-")) return;
  rmSync(directory, { force: true, recursive: true });
}
