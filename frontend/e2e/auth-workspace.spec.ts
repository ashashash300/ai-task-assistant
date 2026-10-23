import { expect, test } from "@playwright/test";

function isRegistrationPath(pathname: string): boolean {
  return pathname === "/api/auth/register" || pathname === "/auth/register";
}

function isCsrfPath(pathname: string): boolean {
  return pathname === "/api/auth/csrf" || pathname === "/auth/csrf";
}

function waitForRegistrationResponse(page: import("@playwright/test").Page) {
  const events: string[] = [];
  let consoleErrorCount = 0;
  let pageErrorCount = 0;
  const recordRequest = (request: import("@playwright/test").Request) => {
    const pathname = new URL(request.url()).pathname;
    if (isRegistrationPath(pathname) || isCsrfPath(pathname)) {
      events.push(`request ${request.method()} ${pathname}`);
    }
  };
  const recordResponse = (response: import("@playwright/test").Response) => {
    const pathname = new URL(response.url()).pathname;
    if (isRegistrationPath(pathname) || isCsrfPath(pathname)) {
      events.push(`response ${response.request().method()} ${pathname} ${response.status()}`);
    }
  };
  const recordFailure = (request: import("@playwright/test").Request) => {
    const pathname = new URL(request.url()).pathname;
    if (isRegistrationPath(pathname) || isCsrfPath(pathname)) {
      events.push(`requestfailed ${pathname} ${request.failure()?.errorText ?? "unknown"}`);
    }
  };
  const recordConsole = (message: import("@playwright/test").ConsoleMessage) => {
    if (message.type() === "error") consoleErrorCount += 1;
  };
  const recordPageError = () => { pageErrorCount += 1; };

  page.on("request", recordRequest);
  page.on("response", recordResponse);
  page.on("requestfailed", recordFailure);
  page.on("console", recordConsole);
  page.on("pageerror", recordPageError);
  return page.waitForResponse((response) => {
    const pathname = new URL(response.url()).pathname;
    return isRegistrationPath(pathname) && response.request().method() === "POST";
  }, { timeout: 25_000 }).catch(() => {
    const summary = events.length > 0 ? events.join(", ") : "none";
    throw new Error(
      `Registration response was not observed; safe endpoint events: ${summary}; ` +
      `console_errors=${consoleErrorCount}; page_errors=${pageErrorCount}`,
    );
  }).finally(() => {
    page.off("request", recordRequest);
    page.off("response", recordResponse);
    page.off("requestfailed", recordFailure);
    page.off("console", recordConsole);
    page.off("pageerror", recordPageError);
  });
}

async function registerAndLogin(page: import("@playwright/test").Page, email: string) {
  await page.goto("/");
  await page.getByRole("tab", { name: "註冊" }).click();
  await page.getByLabel("電子郵件").fill(email);
  await page.getByLabel("密碼", { exact: true }).fill("browser-e2e-password");
  await page.getByLabel("確認密碼", { exact: true }).fill("browser-e2e-password");
  const form = page.locator("form.auth-form");
  await expect(form).toHaveJSProperty("noValidate", false);
  expect(await form.evaluate((element: HTMLFormElement) => element.checkValidity())).toBe(true);
  await expect(page.getByRole("button", { name: "建立帳號" })).toBeEnabled();
  const registerResponse = waitForRegistrationResponse(page);
  await page.getByRole("button", { name: "建立帳號" }).click();
  expect((await registerResponse).status()).toBe(201);
  await expect(page.getByRole("heading", { name: "登入工作區" })).toBeVisible();
  await page.getByLabel("電子郵件").fill(email);
  await page.getByLabel("密碼", { exact: true }).fill("browser-e2e-password");
  await page.getByRole("button", { name: "登入" }).click();
  await expect(page.getByRole("heading", { name: "AI Task Assistant" })).toBeVisible();
}

async function csrfFetch<T>(
  page: import("@playwright/test").Page,
  path: string,
  init: RequestInit = {},
): Promise<{ status: number; body: T | null }> {
  return page.evaluate(async ({ path, init }) => {
    const csrf = await fetch("/api/auth/csrf").then((response) => response.json());
    const response = await fetch(path, {
      ...init,
      headers: { ...init.headers, "X-CSRF-Token": csrf.csrf_token },
    });
    return { status: response.status, body: response.status === 204 ? null : await response.json() };
  }, { path, init });
}

test("browser uses HttpOnly session cookies, CSRF, isolated tasks, mock chat, and logout", async ({ page, context }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "登入工作區" })).toBeVisible();

  await registerAndLogin(page, "browser-a@example.com");
  const cookies = await context.cookies();
  expect(cookies.some((cookie) => cookie.name === "ai_task_session" && cookie.httpOnly)).toBeTruthy();
  await expect.poll(() => page.evaluate(() => fetch("/api/auth/me").then((response) => response.status))).toBe(200);

  const missingCsrf = await page.evaluate(() => fetch("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "blocked" }),
  }).then(async (response) => ({ status: response.status, body: await response.json() })));
  expect(missingCsrf.status).toBe(403);
  expect(missingCsrf.body).toEqual({ detail: { code: "csrf_validation_failed", message: "CSRF validation failed" } });

  const created = await csrfFetch<{ id: number; title: string }>(page, "/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "Browser-owned task" }),
  });
  expect(created.status).toBe(201);
  expect(created.body?.title).toBe("Browser-owned task");
  const taskId = created.body?.id;
  expect(taskId).toBeTruthy();

  const listed = await page.evaluate(() => fetch("/api/tasks").then((response) => response.json()));
  expect(listed).toHaveLength(1);
  const updated = await csrfFetch<{ title: string }>(page, `/api/tasks/${taskId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "Updated browser task" }),
  });
  expect(updated.body?.title).toBe("Updated browser task");

  await page.getByLabel("請告訴 AI 你要處理的任務").fill("測試聊天");
  await page.getByRole("button", { name: "傳送訊息" }).click();
  await expect(page.getByText("Mock AI response")).toBeVisible();
  await page.getByRole("button", { name: "傳送訊息" }).click();
  await expect(page.getByText("Chat quota exceeded")).toBeVisible();

  const removed = await csrfFetch<null>(page, `/api/tasks/${taskId}`, { method: "DELETE" });
  expect(removed.status).toBe(204);

  await page.getByRole("button", { name: "登出" }).click();
  await expect(page.getByRole("heading", { name: "登入工作區" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => fetch("/api/tasks").then((response) => response.status))).toBe(401);
});

test("different browser accounts do not share tasks", async ({ page }) => {
  await registerAndLogin(page, "browser-owner-a@example.com");
  const created = await csrfFetch<{ id: number }>(page, "/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "Private A task" }),
  });
  expect(created.status).toBe(201);
  await page.getByRole("button", { name: "登出" }).click();
  await registerAndLogin(page, "browser-owner-b@example.com");
  const tasks = await page.evaluate(() => fetch("/api/tasks").then((response) => response.json()));
  expect(tasks).toEqual([]);
});
