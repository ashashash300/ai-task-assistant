# AI Task Assistant — Portfolio Summary

## Project description

AI Task Assistant is a full-stack AI application that lets authenticated users manage tasks through REST APIs and natural-language chat. It combines React, FastAPI, SQLAlchemy, SQLite, OpenAI Responses API support, and Google Gemini support while keeping LLM tool calls behind server-side validation and authorization boundaries.

The project focuses on practical AI application engineering: typed tool calling, multi-turn conversation state, secure user isolation, operational guardrails, and regression coverage rather than an unrestricted chatbot demo.

## Resume bullet candidates

- Built a full-stack AI task-management application with a React/TypeScript workspace and FastAPI backend, supporting OpenAI Responses API and Google Gemini through a provider-neutral interface.
- Designed a server-side LLM tool-execution boundary using Pydantic validation and authenticated user-scoped services, preventing providers and tool arguments from selecting database owners directly.
- Implemented SQLite-persisted conversation state for OpenAI response chaining and Gemini structured history, including optimistic locking, retention cleanup, provider isolation, and bounded conversation history.
- Added defense-in-depth browser security controls: Argon2id password hashing, opaque hashed sessions, HttpOnly cookies, Origin validation, double-submit CSRF protection, and owner-scoped Task and Conversation persistence.
- Created regression coverage across pytest, Vitest, and Playwright using isolated temporary SQLite databases and mock AI providers; GitHub Actions runs backend, frontend, and browser E2E checks.

## Main engineering challenges

### Keeping AI useful without giving it database authority

The central design challenge was preserving natural-language task management while ensuring that an LLM could not directly query SQLite, inject arbitrary SQL, select another user, or bypass deletion confirmation. The resulting flow treats function calls as untrusted input and routes every operation through Pydantic schemas and the Task Service.

### Supporting two providers without leaking provider details

OpenAI conversation continuity uses a remote `previous_response_id`, while Gemini requires locally persisted structured history. A small provider contract keeps these formats contained in provider implementations while the shared AI Service owns the system prompt, validated tools, user scope, and task operations.

### Making conversation state safe under persistence and concurrency

Conversation persistence introduces stale writes, provider mismatch, expired histories, and retention races. The application uses user-scoped composite uniqueness, row identity, state-version optimistic locking, JSON history validation, and explicit conflict responses instead of last-write-wins behavior.

### Designing security controls for an AI-facing workflow

Chat can trigger provider calls and Task mutations, so it is protected by the same authenticated identity, Origin checks, and CSRF validation as REST mutations. Server-side request limits, per-user durable daily quota reservations, input limits, provider timeouts, bounded calls, and bounded tool rounds reduce abuse and accidental cost exposure.

## Security design decisions

- Store password hashes with Argon2id and store only hashes of high-entropy session tokens.
- Keep session tokens in HttpOnly cookies rather than localStorage, sessionStorage, URLs, or API response bodies.
- Require an authenticated server-derived `user_id` for Task and Conversation operations; never accept ownership from frontend payloads or LLM arguments.
- Require explicit user confirmation before chat-based deletion and make confirmation single-use, user-scoped, conversation-scoped, and short-lived.
- Distinguish the explicit CSRF validation error from other 403 responses so the frontend can safely retry only the request that the backend rejected before endpoint execution.
- Fail closed when durable quota storage or owner-schema compatibility checks fail.

## Testing and CI highlights

- Backend tests cover authentication, session revocation, CSRF and Origin checks, ownership isolation, tool validation, provider selection, conversation persistence, retention, quota behavior, and migration safety using isolated databases and mocked providers.
- Frontend tests cover authentication state, protected workspace behavior, API error handling, and CSRF retry behavior.
- Playwright E2E tests run against owned localhost FastAPI and Vite processes, a temporary SQLite database, and a test-only mock AI provider. They cover registration, login, CSRF, task operations, mock chat, quota feedback, logout, and account isolation.
- GitHub Actions has separate backend, frontend, and browser E2E jobs. CI does not use real provider API keys.

## Suggested interview talking points

1. **Why not let the model access the database directly?** Explain the tool-validation boundary and how server-side identity is preserved through every Task Service call.
2. **How did conversation persistence differ between OpenAI and Gemini?** Contrast OpenAI's remote response chain with Gemini's locally persisted structured history and versioned JSON codec.
3. **How do you handle concurrent conversation writes?** Discuss optimistic locking, row identity for ABA protection, and why Task side effects and final conversation persistence are intentionally separate transactions.
4. **How did you balance local MVP simplicity with security?** Explain SQLite durable daily quota versus in-memory minute/concurrency limits, and clearly state the single-process deployment boundary.
5. **What would you change for a production deployment?** Cover formal database migration, shared rate-limit/lease infrastructure, HTTPS/reverse-proxy validation, provider budget alerts, and operational monitoring.

## Boundaries to state honestly

- The project is not production-deployed.
- The supported deployment topology is one application process with one SQLite database.
- Daily quota counts admitted chat requests; it is not exact token accounting or a provider billing limit.
- Browser E2E tests use a mock provider and do not prove real-provider behavior or production network configuration.
- The ownership migration workflow is documented and tested in isolation, but has not been applied to a real deployment database.
