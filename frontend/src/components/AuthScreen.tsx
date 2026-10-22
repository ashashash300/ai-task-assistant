import { FormEvent, useState } from "react";

import { getErrorMessage, login, register } from "../api";
import type { User } from "../api";

type Mode = "login" | "register";

type AuthScreenProps = {
  initialNotice?: string | null;
  onAuthenticated: (user: User) => void;
};

const MIN_PASSWORD_LENGTH = 12;
const MAX_PASSWORD_LENGTH = 128;

export function AuthScreen({ initialNotice, onAuthenticated }: AuthScreenProps) {
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(initialNotice ?? null);

  function switchMode(nextMode: Mode) {
    setMode(nextMode);
    setError(null);
    setNotice(null);
    setPassword("");
    setConfirmPassword("");
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isSubmitting) return;
    if (password.length < MIN_PASSWORD_LENGTH || password.length > MAX_PASSWORD_LENGTH) {
      setError(`密碼長度必須介於 ${MIN_PASSWORD_LENGTH} 至 ${MAX_PASSWORD_LENGTH} 字元。`);
      return;
    }
    if (mode === "register" && password !== confirmPassword) {
      setError("兩次輸入的密碼不一致。");
      return;
    }
    setError(null);
    setNotice(null);
    setIsSubmitting(true);
    try {
      const credentials = { email, password };
      if (mode === "register") {
        await register(credentials);
        setPassword("");
        setConfirmPassword("");
        setMode("login");
        setNotice("帳號已建立，請登入以開啟你的任務工作區。");
      } else {
        const user = await login(credentials);
        setPassword("");
        onAuthenticated(user);
      }
    } catch (submitError) {
      setError(getErrorMessage(submitError));
    } finally {
      setIsSubmitting(false);
    }
  }

  const isRegister = mode === "register";
  return (
    <main className="auth-shell">
      <section className="auth-card" aria-labelledby="auth-title">
        <p className="eyebrow">AI Task Assistant</p>
        <h1 id="auth-title">{isRegister ? "建立帳號" : "登入工作區"}</h1>
        <p className="auth-description">以自己的帳號管理任務與 AI 對話，不會共用其他使用者的資料。</p>
        <div className="auth-tabs" role="tablist" aria-label="帳號操作">
          <button type="button" role="tab" aria-selected={!isRegister} className={!isRegister ? "auth-tab active" : "auth-tab"} onClick={() => switchMode("login")}>登入</button>
          <button type="button" role="tab" aria-selected={isRegister} className={isRegister ? "auth-tab active" : "auth-tab"} onClick={() => switchMode("register")}>註冊</button>
        </div>
        {notice && <p className="notice-message" role="status">{notice}</p>}
        {error && <p className="error-message" role="alert">{error}</p>}
        <form className="auth-form" onSubmit={handleSubmit}>
          <label htmlFor="email">電子郵件</label>
          <input id="email" name="email" type="email" autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} required maxLength={320} disabled={isSubmitting} />
          <label htmlFor="password">密碼</label>
          <input id="password" name="password" type="password" autoComplete={isRegister ? "new-password" : "current-password"} value={password} onChange={(event) => setPassword(event.target.value)} required minLength={MIN_PASSWORD_LENGTH} maxLength={MAX_PASSWORD_LENGTH} disabled={isSubmitting} />
          {isRegister && <p className="field-hint">請使用 {MIN_PASSWORD_LENGTH}–{MAX_PASSWORD_LENGTH} 個字元。</p>}
          {isRegister && <><label htmlFor="confirm-password">確認密碼</label><input id="confirm-password" name="confirm-password" type="password" autoComplete="new-password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} required minLength={MIN_PASSWORD_LENGTH} maxLength={MAX_PASSWORD_LENGTH} disabled={isSubmitting} /></>}
          <button type="submit" disabled={isSubmitting}>{isSubmitting ? "處理中…" : isRegister ? "建立帳號" : "登入"}</button>
        </form>
      </section>
    </main>
  );
}
