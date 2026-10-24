import { useEffect, useState } from "react";

import { clearCsrfToken, getCurrentUser, getErrorMessage, isUnauthorizedError } from "./api";
import type { User } from "./api";
import { AuthScreen } from "./components/AuthScreen";
import { Workspace } from "./components/Workspace";

type AuthState = { status: "checking" } | { status: "signed-out"; notice?: string; error?: string } | { status: "signed-in"; user: User };

function App() {
  const [authState, setAuthState] = useState<AuthState>({ status: "checking" });
  useEffect(() => {
    let active = true;
    void getCurrentUser().then((user) => { if (active) setAuthState({ status: "signed-in", user }); }).catch((error) => {
      if (!active) return;
      if (isUnauthorizedError(error)) setAuthState({ status: "signed-out" });
      else setAuthState({ status: "signed-out", error: getErrorMessage(error) });
    });
    return () => { active = false; };
  }, []);
  function handleLoggedOut() { clearCsrfToken(); setAuthState({ status: "signed-out", notice: "你已登出。" }); }
  function handleSessionExpired() { clearCsrfToken(); setAuthState({ status: "signed-out", notice: "工作階段已過期，請重新登入。" }); }
  if (authState.status === "checking") return <main className="auth-shell"><p className="loading-state">正在確認登入狀態…</p></main>;
  if (authState.status === "signed-in") return <Workspace key={authState.user.id} user={authState.user} onLoggedOut={handleLoggedOut} onSessionExpired={handleSessionExpired} />;
  return <AuthScreen initialNotice={authState.notice ?? authState.error} onAuthenticated={(user) => setAuthState({ status: "signed-in", user })} />;
}

export default App;
