import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearCsrfToken, logout, sendChatMessage } from "./client";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

describe("API client", () => {
  beforeEach(() => { clearCsrfToken(); vi.stubGlobal("fetch", vi.fn()); });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("uses credentials and a memory-only CSRF token for chat", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(jsonResponse({ csrf_token: "test-csrf" }));
    fetchMock.mockResolvedValueOnce(jsonResponse({ conversation_id: "workspace-1-x", response: "完成" }));
    await sendChatMessage({ conversation_id: "workspace-1-x", message: "建立任務" });
    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/auth/csrf", expect.objectContaining({ credentials: "include" }));
    const [, options] = fetchMock.mock.calls[1];
    expect(options?.credentials).toBe("include");
    expect(new Headers(options?.headers).get("X-CSRF-Token")).toBe("test-csrf");
  });

  it("handles 204 logout responses without requiring JSON", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(jsonResponse({ csrf_token: "test-csrf" }));
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await expect(logout()).resolves.toBeUndefined();
  });

  it("refreshes the CSRF token once only after the explicit CSRF error code", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(jsonResponse({ csrf_token: "old" }));
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: { code: "csrf_validation_failed", message: "CSRF validation failed" } }, 403));
    fetchMock.mockResolvedValueOnce(jsonResponse({ csrf_token: "new" }));
    fetchMock.mockResolvedValueOnce(jsonResponse({ conversation_id: "workspace-1-x", response: "完成" }));
    await expect(sendChatMessage({ message: "建立任務" })).resolves.toEqual({ conversation_id: "workspace-1-x", response: "完成" });
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("does not retry a non-CSRF 403 mutation", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(jsonResponse({ csrf_token: "token" }));
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "Request origin is not allowed" }, 403));
    await expect(sendChatMessage({ message: "不可重送" })).rejects.toMatchObject({ status: 403 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not retry 401 or 429 responses", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(jsonResponse({ csrf_token: "token" }));
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "Authentication required" }, 401));
    await expect(sendChatMessage({ message: "expired" })).rejects.toMatchObject({ status: 401 });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    clearCsrfToken();
    fetchMock.mockResolvedValueOnce(jsonResponse({ csrf_token: "new-token" }));
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "Chat quota exceeded" }, 429));
    await expect(sendChatMessage({ message: "limited" })).rejects.toMatchObject({ status: 429 });
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });
});
