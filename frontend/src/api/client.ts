import type { ChatRequest, ChatResponse, Task } from "./types";

interface FastAPIValidationIssue {
  loc?: Array<string | number>;
  msg?: string;
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

function isValidationIssues(value: unknown): value is FastAPIValidationIssue[] {
  return Array.isArray(value);
}

function formatErrorDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;

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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;

  try {
    response = await fetch(path, {
      ...init,
      headers: { Accept: "application/json", ...init?.headers },
    });
  } catch (error) {
    if (error instanceof TypeError) {
      throw new ApiError("無法連線至後端服務，請確認 FastAPI 已啟動。");
    }
    throw error;
  }

  const contentType = response.headers.get("content-type") ?? "";
  const body: unknown = contentType.includes("application/json")
    ? await response.json().catch(() => undefined)
    : undefined;

  if (!response.ok) {
    const detail =
      body && typeof body === "object" && "detail" in body
        ? (body as { detail: unknown }).detail
        : undefined;
    throw new ApiError(formatErrorDetail(detail), response.status, detail);
  }

  if (body === undefined) {
    throw new ApiError("伺服器回傳了無法讀取的資料。", response.status);
  }

  return body as T;
}

export function sendChatMessage(requestBody: ChatRequest): Promise<ChatResponse> {
  return request<ChatResponse>("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(requestBody),
  });
}

export function fetchTasks(): Promise<Task[]> {
  return request<Task[]>("/api/tasks");
}

export function getErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "發生未預期的錯誤。";
}
