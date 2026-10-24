# Deployment Security Notes

## Supported MVP topology

This project currently supports **one FastAPI/Uvicorn process connected to one SQLite database**. The Dockerfile starts one Uvicorn process and does not configure workers.

- The daily chat quota is stored in SQLite and survives a process restart.
- The per-minute limiter and per-user in-flight chat guard are in memory. They reset on restart and are not shared by workers, containers, or replicas.
- Do not run multiple Uvicorn workers or replicas and claim the in-flight or minute limits are globally enforced.
- A multi-process deployment requires a shared rate-limit store and a distributed, expiring lease design. This repository does not provide one.

## SQLite quota and schema compatibility

`chat_daily_quotas` stores one atomic request counter for each `(user_id, Asia/Taipei date)`. A reservation is committed before a provider call, including calls that later fail. It counts requests, not exact provider tokens or monetary cost.

FastAPI checks the owner-scoped Task and Conversation schema before `create_all()` runs. A legacy database without ownership columns fails closed, so startup does not add the quota table to that incompatible database. Run the reviewed ownership migration only during a maintenance window and only after the application ORM/services match its target schema. The migration tool does not run automatically.

After an owner-scoped migration has been verified, normal startup may create the previously absent quota table. This is table creation, not a substitute for future schema migrations. Keep SQLite backups outside the repository and follow [the ownership migration procedure](ownership-migration.md).

## HTTPS, cookies, origins, and reverse proxies

- Use HTTPS in production and keep `SESSION_COOKIE_SECURE=true`.
- Keep session cookies HttpOnly and do not place session tokens in browser storage or URLs.
- Set `AUTH_ALLOWED_ORIGINS` to explicit HTTPS origins, for example `https://app.example.com`. Do not use `*` with credentials.
- The application deliberately uses the direct ASGI peer (`Request.client.host`) for IP limits and does not parse arbitrary `X-Forwarded-For` headers.
- If a reverse proxy terminates connections, only enable Uvicorn proxy headers after determining the proxy's exact trusted IP address or CIDR. Use an explicit configuration such as `--proxy-headers --forwarded-allow-ips=<known-proxy-address>`; never use `--forwarded-allow-ips="*"`.
- Do not guess proxy addresses. Validate the actual network path, proxy header behavior, HTTPS termination, and browser Origin behavior in the deployment environment.

## Provider cost and availability controls

The application limits input size, provider calls per chat, tool rounds, request timeout, and requested output tokens. It does not currently persist a provider-neutral token-usage record or enforce a monetary budget.

Before using a paid provider in production, configure account-level budget caps, usage alerts, and billing notifications with the selected provider. Treat the SQLite daily request quota as an application abuse guard, not as exact spend accounting.

## Required deployment validation

Before any public deployment, verify in the real environment:

1. HTTPS, Secure cookies, allowed Origins, CSRF, and session expiry in a real browser.
2. The exact reverse-proxy trust boundary and client-IP behavior.
3. SQLite backup, restore, integrity checks, and disk permissions.
4. Provider budget alerts and failure behavior without logging keys, prompts, cookies, or tokens.
5. A multi-worker/replica replacement plan if horizontal scaling is required.

## Browser E2E test environment

The Playwright configuration starts one owned local E2E launcher on `127.0.0.1`. The launcher first refuses occupied ports, then starts its own FastAPI and Vite child processes. It tracks only those child process handles and terminates them on normal completion, startup failure, or browser-launch failure; it never uses broad `pkill`/`killall` commands or attaches to an existing service.

It uses a temporary SQLite file, disables `.env` loading through `AI_TASK_ASSISTANT_DISABLE_DOTENV=1`, and starts `scripts/browser_e2e_server.py`, which requires an explicit E2E instance identifier and substitutes a test-only mock provider. It never calls OpenAI or Gemini.

Run it locally with:

```bash
cd frontend
npx playwright install chromium
npm run test:e2e
```

GitHub Actions installs Chromium with its Linux dependencies and runs the same E2E command in a separate `browser-e2e` job. The runner cleans its generated temporary SQLite directory after execution. Playwright trace and report output are ignored by Git. A passing local or CI E2E run validates only the mock-provider localhost flow; it does not validate production HTTPS, reverse-proxy trust, or real-provider integration.
