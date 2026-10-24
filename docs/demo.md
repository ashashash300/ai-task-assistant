# Deterministic Interview Demo

## Purpose

This launcher provides a short, human-operated portfolio demo without using OpenAI or Gemini credentials, the development SQLite database, or Playwright Chromium. It starts a temporary loopback-only FastAPI backend and Vite frontend, then replaces the normal provider factory in that **demo process only** with a deterministic provider that exercises the existing validated tool callback.

It is a demonstration of the application's tool-execution and authorization boundaries. It is **not** real LLM inference and it is not a production deployment mode.

## Prerequisites

- Python 3.12 virtual environment with project dependencies installed.
- Frontend dependencies installed once with `npm install` from `frontend/`.
- Ports `8001` and `5173` available on `127.0.0.1`.
- A normal browser such as Chrome or Safari for manual interaction.

Do not create or edit `.env` for this demo. The launcher explicitly disables dotenv loading and provides test-only placeholder provider keys.

## Start the demo

From the repository root, run:

```bash
PYTHONPATH=src .venv/bin/python scripts/run_demo.py
```

The launcher creates a new temporary directory and SQLite file, verifies that both loopback ports are free, starts only processes it owns, creates two temporary accounts through the real protected registration route, and prints the local URL plus runtime-only credentials.

Open the URL printed by the launcher, normally:

```text
http://127.0.0.1:5173
```

Press `Ctrl+C` in the launcher terminal when the demo is complete. It sends termination signals only to the backend and Vite process groups it created, then removes its own temporary directory and database.

## Credentials and temporary data

- Account A and Account B emails include random suffixes.
- Passwords are generated with `secrets.token_urlsafe()` at runtime.
- Credentials are printed only to the local launcher terminal; they are not written to a source file, `.env`, or application log.
- Both accounts, sessions, conversations, tasks, and quota rows exist only in the temporary SQLite database and disappear on cleanup.

Do not copy these credentials into documentation, shell history, screenshots, or commits.

## Supported deterministic chat commands

The demo provider accepts **only** these exact commands:

| Command | Server-side behavior |
| --- | --- |
| `建立面試準備任務` | Calls the existing validated `create_task` tool callback with a fixed demo title, description, and high priority. |
| `完成剛剛的任務` | Calls the existing validated `list_tasks` callback, selects the newest pending task with the deterministic demo title and description from the authenticated user's list, then calls `complete_task`. |

Other messages return a deterministic help response and do not invoke any tool. The provider never calls delete tools, never receives a client-provided `user_id`, and never opens SQLite directly.

## Two-account isolation walkthrough

1. Log in as **Account A**.
2. Send `建立面試準備任務`; the task appears in Account A's list.
3. Send `完成剛剛的任務`; the same task becomes completed.
4. Log out using the UI.
5. Log in as **Account B**.
6. Confirm that Account B's task list is empty and cannot see Account A's task.

This uses the normal session, CSRF, Origin validation, Task Service ownership filters, conversation scope, rate limiter, and SQLite daily quota. Demo mode does not add an unauthenticated task endpoint or relax these controls.

## Isolation and safety properties

- `scripts/demo_server.py` refuses to start without `AI_TASK_ASSISTANT_DEMO=1`, a valid random instance identifier, an absolute temporary database path, matching SQLite URL, placeholder provider keys, disabled dotenv loading, loopback ports, and an exact Vite Origin.
- The normal `ai_task_assistant.main` entry point never installs the demo provider.
- The demo database must live under a launcher-owned temporary directory and is rejected if it resolves to `ai_task_assistant.db`.
- FastAPI binds only to `127.0.0.1`; Vite also binds only to `127.0.0.1`.
- The launcher refuses occupied ports instead of attaching to another local service.
- `SESSION_COOKIE_SECURE=false` is limited to the explicit local HTTP demo process. Production defaults remain unchanged.
- The demo sets a daily chat quota of five so the two demonstration tool calls can complete. It does not remove quota, rate-limit, ownership, session, CSRF, or Origin protections.

## macOS Playwright limitation

Some local macOS environments have encountered Playwright Chromium `SIGTRAP` / `EPERM` sandbox failures. This launcher does **not** start Playwright and does not require Chromium automation. Use a normal manually opened browser for the demo.

The project still has browser E2E coverage in CI. A successful mock E2E run does not validate real LLM behavior, HTTPS deployment, reverse proxy behavior, or the local macOS Playwright sandbox.

## Mock tool calling versus real LLM inference

| Deterministic demo | Normal application provider |
| --- | --- |
| Fixed commands and fixed tool selection | OpenAI Responses API or Gemini `generate_content` selects tool calls from natural language. |
| No network Provider request | May call the configured external provider. |
| Placeholder provider keys | Requires the selected provider's real backend-only key. |
| Demonstrates validation, ownership, Task Service, CSRF, session, and persistence boundaries | Demonstrates the same boundaries plus provider reasoning and model availability behavior. |

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `Frontend dependencies are missing` | Run `cd frontend && npm install`, then retry from the repository root. |
| A loopback port is already in use | Stop the process you intentionally own on port `8001` or `5173`; the launcher will not terminate it. |
| Demo backend or frontend does not become ready | Review the local terminal output, stop with `Ctrl+C`, and retry. Do not point the launcher at an existing development server. |
| Browser cannot log in | Use the exact `127.0.0.1:5173` URL printed by the launcher; do not substitute `localhost`, because Origin validation is exact. |
| A chat command shows help text | Use one of the two exact supported commands listed above. |

## Boundaries

- The launcher is for local portfolio demonstrations only.
- It is not a migration, deployment, load-testing, or production-support tool.
- It does not contact OpenAI or Gemini.
- It does not validate production HTTPS, reverse proxies, multi-worker deployment, or provider billing behavior.
