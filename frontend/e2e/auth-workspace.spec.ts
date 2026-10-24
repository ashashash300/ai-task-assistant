import { expect, test } from "@playwright/test";

type Page = import("@playwright/test").Page;
type Request = import("@playwright/test").Request;
type Response = import("@playwright/test").Response;
type ConsoleMessage = import("@playwright/test").ConsoleMessage;

type E2EDiagnostics = {
  runPhase: <T>(phase: string, action: () => Promise<T>) => Promise<T>;
  authUiStatus: () => Promise<void>;
  dispose: () => void;
};

function logDiagnostic(testId: string, message: string): void {
  console.log(`[e2e][${testId}] ${message}`);
}

function isRegistrationPath(pathname: string): boolean {
  return pathname === "/api/auth/register" || pathname === "/auth/register";
}

function isCsrfPath(pathname: string): boolean {
  return pathname === "/api/auth/csrf" || pathname === "/auth/csrf";
}

function isAuthPath(pathname: string): boolean {
  return isRegistrationPath(pathname) || isCsrfPath(pathname) ||
    pathname === "/api/auth/me" || pathname === "/auth/me" ||
    pathname === "/api/auth/login" || pathname === "/auth/login" ||
    pathname === "/api/auth/logout" || pathname === "/auth/logout" ||
    pathname === "/api/chat" || pathname === "/chat";
}

function createDiagnostics(page: Page, testId: string): E2EDiagnostics {
  let consoleErrorCount = 0;
  let pageErrorCount = 0;
  const recordRequest = (request: Request) => {
    const pathname = new URL(request.url()).pathname;
    if (isAuthPath(pathname)) {
      logDiagnostic(testId, `network request ${request.method()} ${pathname}`);
    }
  };
  const recordResponse = (response: Response) => {
    const pathname = new URL(response.url()).pathname;
    if (isAuthPath(pathname)) {
      logDiagnostic(testId, `network response ${response.request().method()} ${pathname} ${response.status()}`);
    }
  };
  const recordFailure = (request: Request) => {
    const pathname = new URL(request.url()).pathname;
    if (isAuthPath(pathname)) {
      // Browser failure text can include implementation-specific details. Keep
      // CI diagnostics to a fixed category instead of emitting it verbatim.
      logDiagnostic(testId, `network requestfailed ${request.method()} ${pathname} network_error`);
    }
  };
  const recordConsole = (message: ConsoleMessage) => {
    if (message.type() === "error") consoleErrorCount += 1;
  };
  const recordPageError = () => { pageErrorCount += 1; };

  page.on("request", recordRequest);
  page.on("response", recordResponse);
  page.on("requestfailed", recordFailure);
  page.on("console", recordConsole);
  page.on("pageerror", recordPageError);
  const authUiStatus = async (): Promise<void> => {
    const loginHeading = await page.getByRole("heading", { name: "登入工作區", exact: true }).isVisible();
    const registerTab = await page.getByRole("tab", { name: "註冊", exact: true }).isVisible();
    const loading = await page.getByText("正在確認登入狀態…", { exact: true }).isVisible();
    const workspace = await page.getByRole("heading", { name: "AI Task Assistant", exact: true }).isVisible();
    logDiagnostic(
      testId,
      `auth_ui login_heading=${loginHeading} register_tab=${registerTab} loading=${loading} workspace=${workspace} ` +
        `console_errors=${consoleErrorCount} page_errors=${pageErrorCount}`,
    );
  };
  return {
    async runPhase<T>(phase: string, action: () => Promise<T>): Promise<T> {
      const startedAt = Date.now();
      logDiagnostic(testId, `phase ${phase} START`);
      try {
        const result = await action();
        logDiagnostic(testId, `phase ${phase} COMPLETE duration_ms=${Date.now() - startedAt}`);
        return result;
      } catch (error) {
        logDiagnostic(testId, `phase ${phase} FAILED duration_ms=${Date.now() - startedAt}`);
        try {
          await authUiStatus();
        } catch {
          logDiagnostic(testId, "auth_ui status_unavailable");
        }
        throw error;
      }
    },
    authUiStatus,
    dispose(): void {
      page.off("request", recordRequest);
      page.off("response", recordResponse);
      page.off("requestfailed", recordFailure);
      page.off("console", recordConsole);
      page.off("pageerror", recordPageError);
    },
  };
}

async function registerAndLogin(page: Page, diagnostics: E2EDiagnostics, email: string) {
  await diagnostics.runPhase("navigation", () => page.goto("/"));
  await diagnostics.runPhase("auth_ui_visible", async () => {
    await expect(page.getByRole("heading", { name: "登入工作區", exact: true })).toBeVisible();
    await diagnostics.authUiStatus();
  });
  await diagnostics.runPhase("registration_form_ready", async () => {
    await page.getByRole("tab", { name: "註冊", exact: true }).click();
    await page.getByLabel("電子郵件", { exact: true }).fill(email);
    await page.getByLabel("密碼", { exact: true }).fill("browser-e2e-password");
    await page.getByLabel("確認密碼", { exact: true }).fill("browser-e2e-password");
    const form = page.locator("form.auth-form");
    await expect(form).toHaveJSProperty("noValidate", false);
    expect(await form.evaluate((element: HTMLFormElement) => element.checkValidity())).toBe(true);
    await expect(page.getByRole("button", { name: "建立帳號" })).toBeEnabled();
  });
  const csrfResponse = page.waitForResponse((response) => {
    const pathname = new URL(response.url()).pathname;
    return isCsrfPath(pathname) && response.request().method() === "GET";
  }, { timeout: 25_000 });
  const registerResponse = page.waitForResponse((response) => {
    const pathname = new URL(response.url()).pathname;
    return isRegistrationPath(pathname) && response.request().method() === "POST";
  }, { timeout: 25_000 });
  await diagnostics.runPhase("csrf_get", async () => {
    await page.getByRole("button", { name: "建立帳號", exact: true }).click();
    expect((await csrfResponse).status()).toBe(200);
  });
  await diagnostics.runPhase("registration_post", async () => {
    expect((await registerResponse).status()).toBe(201);
  });
  await diagnostics.runPhase("login", async () => {
    await expect(page.getByRole("heading", { name: "登入工作區", exact: true })).toBeVisible();
    await page.getByLabel("電子郵件", { exact: true }).fill(email);
    await page.getByLabel("密碼", { exact: true }).fill("browser-e2e-password");
    await page.getByRole("button", { name: "登入", exact: true }).click();
  });
  await diagnostics.runPhase("workspace_ready", async () => {
    await expect(page.getByRole("heading", { name: "AI Task Assistant", exact: true })).toBeVisible();
  });
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

function isExpectedPathResponse(response: Response, pathname: string, method: string): boolean {
  return new URL(response.url()).pathname === pathname && response.request().method() === method;
}

async function isCsrfValidationFailure(response: Response): Promise<boolean> {
  if (response.status() !== 403) return false;
  const body: unknown = await response.json().catch(() => undefined);
  return typeof body === "object" && body !== null && "detail" in body &&
    typeof (body as { detail?: unknown }).detail === "object" &&
    (body as { detail: { code?: unknown } }).detail.code === "csrf_validation_failed";
}

function waitForFinalMutationResponse(
  page: Page,
  pathname: string,
  method: string,
  expectedStatus: number,
): Promise<Response> {
  return page.waitForResponse(async (response) => {
    if (!isExpectedPathResponse(response, pathname, method)) return false;
    if (response.status() === expectedStatus) return true;
    // Only this dependency-level failure is followed by the client's one-time
    // token refresh and retry. Any other response resolves immediately so the
    // existing status assertion fails rather than being hidden by a timeout.
    return !(await isCsrfValidationFailure(response));
  });
}

async function logoutAndVerify(page: Page, context: import("@playwright/test").BrowserContext): Promise<void> {
  const logoutResponse = waitForFinalMutationResponse(page, "/api/auth/logout", "POST", 204);
  await page.getByRole("button", { name: "登出", exact: true }).click();
  expect((await logoutResponse).status()).toBe(204);
  await expect.poll(() => page.evaluate(() => fetch("/api/auth/me").then((response) => response.status))).toBe(401);
  const cookies = await context.cookies();
  expect(cookies.some((cookie) => cookie.name === "ai_task_session")).toBeFalsy();
}

test("browser uses HttpOnly session cookies, CSRF, isolated tasks, mock chat, and logout", async ({ page, context }) => {
  const diagnostics = createDiagnostics(page, "workspace_flow");
  try {
    await registerAndLogin(page, diagnostics, "browser-a@example.com");
    await diagnostics.runPhase("session_cookie", async () => {
      const cookies = await context.cookies();
      expect(cookies.some((cookie) => cookie.name === "ai_task_session" && cookie.httpOnly)).toBeTruthy();
    });
    await diagnostics.runPhase("authenticated_me", async () => {
      await expect.poll(() => page.evaluate(() => fetch("/api/auth/me").then((response) => response.status))).toBe(200);
    });

    await diagnostics.runPhase("csrf_protection", async () => {
      const missingCsrf = await page.evaluate(() => fetch("/api/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: "blocked" }),
      }).then(async (response) => ({ status: response.status, body: await response.json() })));
      expect(missingCsrf.status).toBe(403);
      expect(missingCsrf.body).toEqual({ detail: { code: "csrf_validation_failed", message: "CSRF validation failed" } });
    });

    await diagnostics.runPhase("task_crud", async () => {
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
      await csrfFetch<null>(page, `/api/tasks/${taskId}`, { method: "DELETE" }).then((removed) => {
        expect(removed.status).toBe(204);
      });
    });

    await diagnostics.runPhase("mock_chat", async () => {
      const chatResponse = waitForFinalMutationResponse(page, "/api/chat", "POST", 200);
      await page.getByLabel("請告訴 AI 你要處理的任務").fill("測試聊天");
      await page.getByRole("button", { name: "傳送訊息" }).click();
      expect((await chatResponse).status()).toBe(200);
      await expect(page.getByText("Mock AI response")).toBeVisible();
      await expect(page.getByRole("button", { name: "傳送訊息", exact: true })).toBeEnabled();
    });
    await diagnostics.runPhase("quota", async () => {
      const quotaResponse = waitForFinalMutationResponse(page, "/api/chat", "POST", 429);
      await page.getByRole("button", { name: "傳送訊息" }).click();
      expect((await quotaResponse).status()).toBe(429);
      await expect(page.getByText("Chat quota exceeded")).toBeVisible();
    });

    await diagnostics.runPhase("logout", async () => {
      await logoutAndVerify(page, context);
      await expect(page.getByRole("heading", { name: "登入工作區" })).toBeVisible();
      await expect.poll(() => page.evaluate(() => fetch("/api/tasks").then((response) => response.status))).toBe(401);
    });
  } finally {
    diagnostics.dispose();
  }
});

test("different browser accounts do not share tasks", async ({ page, context }) => {
  const diagnostics = createDiagnostics(page, "account_isolation");
  try {
    await registerAndLogin(page, diagnostics, "browser-owner-a@example.com");
    await diagnostics.runPhase("task_crud", async () => {
      const created = await csrfFetch<{ id: number }>(page, "/api/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: "Private A task" }),
      });
      expect(created.status).toBe(201);
    });
    await diagnostics.runPhase("logout", async () => {
      await logoutAndVerify(page, context);
    });
    await registerAndLogin(page, diagnostics, "browser-owner-b@example.com");
    await diagnostics.runPhase("account_isolation", async () => {
      const tasks = await page.evaluate(() => fetch("/api/tasks").then((response) => response.json()));
      expect(tasks).toEqual([]);
    });
  } finally {
    diagnostics.dispose();
  }
});
