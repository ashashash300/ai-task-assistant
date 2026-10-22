import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./api/client";

const api = vi.hoisted(() => ({
  clearCsrfToken: vi.fn(),
  fetchTasks: vi.fn(),
  getCurrentUser: vi.fn(),
  getErrorMessage: (error: unknown) => error instanceof Error ? error.message : "錯誤",
  isUnauthorizedError: (error: unknown) => error instanceof ApiError && error.status === 401,
  login: vi.fn(),
  logout: vi.fn(),
  register: vi.fn(),
  sendChatMessage: vi.fn(),
}));

vi.mock("./api", () => api);

import App from "./App";

describe("App authentication state", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getCurrentUser.mockRejectedValue(new ApiError("未登入", 401));
    api.fetchTasks.mockResolvedValue([]);
    api.logout.mockResolvedValue(undefined);
  });

  it("shows Login and does not load tasks when unauthenticated", async () => {
    render(<App />);
    expect(await screen.findByRole("heading", { name: "登入工作區" })).toBeInTheDocument();
    expect(api.fetchTasks).not.toHaveBeenCalled();
  });

  it("shows the workspace after /auth/me succeeds", async () => {
    api.getCurrentUser.mockResolvedValue({ id: 1, email: "user@example.com" });
    render(<App />);
    expect(await screen.findByRole("heading", { name: "AI Task Assistant" })).toBeInTheDocument();
    await waitFor(() => expect(api.fetchTasks).toHaveBeenCalled());
  });

  it("validates confirmation before register request", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "登入工作區" });
    fireEvent.click(screen.getByRole("tab", { name: "註冊" }));
    fireEvent.change(screen.getByLabelText("電子郵件"), { target: { value: "new@example.com" } });
    fireEvent.change(screen.getByLabelText("密碼"), { target: { value: "123456789012" } });
    fireEvent.change(screen.getByLabelText("確認密碼"), { target: { value: "different-password" } });
    fireEvent.click(screen.getByRole("button", { name: "建立帳號" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("兩次輸入的密碼不一致");
    expect(api.register).not.toHaveBeenCalled();
  });

  it("logs in and begins a fresh user-scoped workspace", async () => {
    api.login.mockResolvedValue({ id: 2, email: "second@example.com" });
    render(<App />);
    await screen.findByRole("heading", { name: "登入工作區" });
    fireEvent.change(screen.getByLabelText("電子郵件"), { target: { value: "second@example.com" } });
    fireEvent.change(screen.getByLabelText("密碼"), { target: { value: "123456789012" } });
    fireEvent.click(screen.getByRole("button", { name: "登入" }));
    expect(await screen.findByText("second@example.com")).toBeInTheDocument();
    expect(api.login).toHaveBeenCalledWith({ email: "second@example.com", password: "123456789012" });
  });

  it("returns to Login after session expiry but not after a 403", async () => {
    api.getCurrentUser.mockResolvedValue({ id: 1, email: "user@example.com" });
    api.fetchTasks.mockRejectedValueOnce(new ApiError("CSRF validation failed", 403));
    render(<App />);
    expect(await screen.findByText("CSRF validation failed")).toBeInTheDocument();
    expect(screen.getByText("user@example.com")).toBeInTheDocument();
  });
});
