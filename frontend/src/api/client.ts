import type {
  ChatRequest,
  ChatResponse,
  CredentialsRequest,
  CsrfTokenResponse,
  Task,
  User,
} from "./types";

interface FastAPIValidationIssue {
  loc?: Array<string | number>;
  msg?: string;
}

interface CsrfErrorDetail {
  code: "csrf_validation_failed";
  message: string;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status?: number,
    public readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

let csrfToken: string | null = null;
let csrfRequest: Promise<string> | null = null;

function isValidationIssues(value: unknown): value is FastAPIValidationIssue[] {
  return Array.isArray(value);
}

function formatErrorDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;

  if (isCsrfErrorDetail(detail)) return detail.message;

  if (isValidationIssues(detail)) {
    return detail
      .map((issue) => {
        const field = issue.loc?.slice(1).join(".");
        return field && issue.msg ? `${field}: ${issue.msg}` : issue.msg;
      })
      .filter((message): message is string => Boolean(message))
      .join("；");
  }

  return "伺服器無法完成這項請求。";
}

function isCsrfErrorDetail(detail: unknown): detail is CsrfErrorDetail {
  return typeof detail === "object" && detail !== null &&
    "code" in detail && (detail as { code?: unknown }).code === "csrf_validation_failed" &&
    "message" in detail && typeof (detail as { message?: unknown }).message === "string";
}

type RequestOptions = {
  csrf?: boolean;
  retryCsrf?: boolean;
};

async function fetchResponse(path: string, init?: RequestInit): Promise<Response> {
  let response: Response;

  try {
    response = await fetch(path, {
      ...init,
      credentials: "include",
      headers: init?.headers ?? { Accept: "application/json" },
    });
  } catch (error) {
    if (error instanceof TypeError) {
      throw new ApiError("無法連線至後端服務，請確認 FastAPI 已啟動。");
    }
    throw error;
  }

  return response;
}

async function readResponseBody(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) return undefined;
  return response.json().catch(() => undefined);
}

async function request<T>(
  path: string,
  init?: RequestInit,
  options: RequestOptions = {},
): Promise<T | undefined> {
  const headers = new Headers(init?.headers);
  headers.set("Accept", "application/json");
  if (options.csrf) {
    headers.set("X-CSRF-Token", await getCsrfToken());
  }

  const response = await fetchResponse(path, { ...init, headers });
  const body = await readResponseBody(response);
  // Only the explicit dependency-level CSRF failure is safe to retry: FastAPI
  // rejected it before endpoint execution, so the original mutation did not run.
  if (
    response.status === 403 &&
    options.csrf &&
    options.retryCsrf !== false &&
    isCsrfErrorDetail(body && typeof body === "object" && "detail" in body
      ? (body as { detail: unknown }).detail
      : undefined)
  ) {
    csrfToken = null;
    return request<T>(path, init, { csrf: true, retryCsrf: false });
  }

  if (!response.ok) {
    const detail =
      body && typeof body === "object" && "detail" in body
        ? (body as { detail: unknown }).detail
        : undefined;
    throw new ApiError(formatErrorDetail(detail), response.status, detail);
  }

  if (response.status === 204) return undefined;
  if (body === undefined) throw new ApiError("伺服器回傳了無法讀取的資料。", response.status);

  return body as T;
}

export async function getCsrfToken(): Promise<string> {
  if (csrfToken) return csrfToken;
  if (!csrfRequest) {
    csrfRequest = (async () => {
      const response = await fetchResponse("/api/auth/csrf");
      const body = await readResponseBody(response);
      if (!response.ok) {
        const detail = body && typeof body === "object" && "detail" in body
          ? (body as { detail: unknown }).detail
          : undefined;
        throw new ApiError(formatErrorDetail(detail), response.status, detail);
      }
      const token = (body as CsrfTokenResponse | undefined)?.csrf_token;
      if (!token) throw new ApiError("無法取得安全驗證資訊。", response.status);
      csrfToken = token;
      return token;
    })().finally(() => {
      csrfRequest = null;
    });
  }
  return csrfRequest;
}

export function clearCsrfToken(): void {
  csrfToken = null;
}

export function sendChatMessage(requestBody: ChatRequest): Promise<ChatResponse> {
  return request<ChatResponse>("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(requestBody),
  }, { csrf: true }).then(requireBody);
}

export function fetchTasks(): Promise<Task[]> {
  return request<Task[]>("/api/tasks").then(requireBody);
}

export function register(credentials: CredentialsRequest): Promise<User> {
  return request<User>("/api/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(credentials),
  }, { csrf: true }).then(requireBody);
}

export function login(credentials: CredentialsRequest): Promise<User> {
  return request<User>("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(credentials),
  }, { csrf: true }).then(requireBody);
}

export function getCurrentUser(): Promise<User> {
  return request<User>("/api/auth/me").then(requireBody);
}

export async function logout(): Promise<void> {
  await request<void>("/api/auth/logout", { method: "POST" }, { csrf: true });
  clearCsrfToken();
}

function requireBody<T>(body: T | undefined): T {
  if (body === undefined) throw new ApiError("伺服器回傳了空白資料。");
  return body;
}

export function isUnauthorizedError(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 401;
}

export function getErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "發生未預期的錯誤。";
}
