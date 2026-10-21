import { FormEvent, KeyboardEvent, useCallback, useEffect, useState } from "react";

import { fetchTasks, getErrorMessage, sendChatMessage } from "./api";
import type { Task } from "./api";

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
};

const conversationId = `demo-${crypto.randomUUID()}`;

const priorityLabels = { low: "低", medium: "中", high: "高" } as const;
const statusLabels = { pending: "待處理", completed: "已完成", cancelled: "已取消" } as const;

function formatTaipeiDate(value: string | null): string {
  if (!value) return "無截止日期";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "日期資料無法顯示";
  return new Intl.DateTimeFormat("zh-TW", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Taipei",
  }).format(date);
}

function App() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [message, setMessage] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [chatError, setChatError] = useState<string | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [isTasksLoading, setIsTasksLoading] = useState(true);
  const [tasksError, setTasksError] = useState<string | null>(null);

  const loadTasks = useCallback(async () => {
    setIsTasksLoading(true);
    setTasksError(null);
    try {
      setTasks(await fetchTasks());
    } catch (error) {
      setTasksError(getErrorMessage(error));
    } finally {
      setIsTasksLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadTasks();
  }, [loadTasks]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedMessage = message.trim();
    if (!trimmedMessage || isSending) return;

    setChatError(null);
    setMessages((current) => [...current, { id: crypto.randomUUID(), role: "user", content: trimmedMessage }]);
    setMessage("");
    setIsSending(true);

    try {
      const response = await sendChatMessage({ conversation_id: conversationId, message: trimmedMessage });
      setMessages((current) => [...current, { id: crypto.randomUUID(), role: "assistant", content: response.response }]);
      await loadTasks();
    } catch (error) {
      setChatError(getErrorMessage(error));
    } finally {
      setIsSending(false);
    }
  }

  function handleMessageKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  }

  return (
    <main className="app-shell">
      <header className="page-header">
        <p className="eyebrow">Phase 9 · Task workspace</p>
        <h1>AI Task Assistant</h1>
        <p>以自然語言管理任務，所有時間皆以台北時區呈現。</p>
      </header>

      <div className="workspace-grid">
        <section className="panel chat-panel" aria-labelledby="chat-title">
          <div className="panel-heading">
            <div><p className="section-kicker">AI Assistant</p><h2 id="chat-title">對話管理任務</h2></div>
            <span className="conversation-badge" title="此頁面重新整理後會開始新會話">本頁會話中</span>
          </div>
          <div className="message-list" aria-live="polite" aria-label="聊天訊息">
            {messages.length === 0 ? <p className="empty-chat">試著說：「明天下午三點提醒我準備 AI 面試」</p> : messages.map((chatMessage) => (
              <article key={chatMessage.id} className={`message message-${chatMessage.role}`}>
                <strong>{chatMessage.role === "user" ? "你" : "AI 助手"}</strong><p>{chatMessage.content}</p>
              </article>
            ))}
            {isSending && <p className="typing-indicator">AI 助手正在處理…</p>}
          </div>
          {chatError && <p className="error-message" role="alert">{chatError}</p>}
          <form className="chat-form" onSubmit={handleSubmit}>
            <label htmlFor="chat-message">請告訴 AI 你要處理的任務</label>
            <textarea id="chat-message" value={message} onChange={(event) => setMessage(event.target.value)} onKeyDown={handleMessageKeyDown} placeholder="例如：把明天的面試準備改成晚上八點" rows={3} disabled={isSending} maxLength={4000} />
            <div className="form-footer"><span>Enter 傳送 · Shift + Enter 換行</span><button type="submit" disabled={isSending || !message.trim()}>{isSending ? "傳送中…" : "傳送訊息"}</button></div>
          </form>
        </section>

        <section className="panel tasks-panel" aria-labelledby="tasks-title">
          <div className="panel-heading">
            <div><p className="section-kicker">Tasks</p><h2 id="tasks-title">目前待辦</h2></div>
            <button className="secondary-button" type="button" onClick={() => void loadTasks()} disabled={isTasksLoading}>{isTasksLoading ? "載入中…" : "重新整理"}</button>
          </div>
          {tasksError && <div className="error-block" role="alert"><p>{tasksError}</p><button type="button" onClick={() => void loadTasks()}>重試</button></div>}
          {isTasksLoading ? <p className="loading-state">正在載入任務…</p> : !tasksError && tasks.length === 0 ? <p className="empty-state">目前沒有任務。使用左側對話建立第一個待辦吧。</p> : (
            <ul className="task-list" aria-label="任務清單">
              {tasks.map((task) => <li key={task.id} className="task-card">
                <div className="task-card-header"><h3>{task.title}</h3><span className={`status status-${task.status}`}>{statusLabels[task.status]}</span></div>
                <p className="task-description">{task.description || "沒有描述"}</p>
                <dl className="task-metadata"><div><dt>截止時間</dt><dd>{formatTaipeiDate(task.due_date)}</dd></div><div><dt>優先順序</dt><dd>{priorityLabels[task.priority]}</dd></div></dl>
              </li>)}
            </ul>
          )}
        </section>
      </div>
    </main>
  );
}

export default App;
