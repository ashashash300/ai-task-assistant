export type TaskPriority = "low" | "medium" | "high";
export type TaskStatus = "pending" | "completed" | "cancelled";

export interface ChatRequest {
  conversation_id?: string;
  message: string;
}

export interface ChatResponse {
  conversation_id?: string;
  response: string;
}

export interface Task {
  id: number;
  title: string;
  description: string | null;
  due_date: string | null;
  priority: TaskPriority;
  status: TaskStatus;
  created_at: string;
  updated_at: string;
}
