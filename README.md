# AI Task Assistant

A simple backend API for an AI-powered task management application. This project is being built as a junior AI Software Engineer portfolio project, starting with a clean FastAPI foundation.

## Current tech stack

- Python 3.12
- FastAPI
- Uvicorn
- SQLAlchemy with SQLite
- Pydantic Settings
- pytest and httpx

## Project structure

```text
ai-task-assistant/
├── pyproject.toml
├── src/
│   └── ai_task_assistant/
│       ├── main.py          # FastAPI application
│       ├── api/routes/      # API route modules
│       ├── core/            # Application configuration
│       ├── db/              # Database setup
│       ├── models/          # SQLAlchemy models
│       ├── schemas/         # Pydantic schemas
│       └── services/        # Application services
└── tests/                   # pytest test suite
```

## Current API endpoints

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Confirm the API is running |
| `POST` | `/tasks` | Create a task |
| `GET` | `/tasks` | List all tasks |
| `GET` | `/tasks/{task_id}` | Get one task |
| `PATCH` | `/tasks/{task_id}` | Update task fields |
| `DELETE` | `/tasks/{task_id}` | Delete a task |
| `POST` | `/chat` | Manage tasks with natural-language requests |

The SQLite database is created automatically when the API starts. Task CRUD and LLM tool calling are available.

## Chat configuration

The `/chat` endpoint uses OpenAI function calling to request validated task operations. Copy `.env.example` to `.env` and set `OPENAI_API_KEY`. Do not commit `.env`.

## Conversation memory

`POST /chat` remains stateless when `conversation_id` is omitted. To continue a conversation, send a valid ID containing letters, numbers, hyphens, or underscores (maximum 64 characters):

```json
{
  "conversation_id": "demo-001",
  "message": "改成晚上八點"
}
```

Conversation memory is in application memory only. It stores the latest OpenAI response ID for each conversation, supports up to 100 conversations and 10 turns per conversation, and is lost when the application restarts. This demo does not implement authentication, so conversation IDs must not be treated as private access controls.

## Local setup

Python 3.12 is required.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Copy the example environment file when configuration is needed:

```bash
cp .env.example .env
```

## Run the API

```bash
uvicorn ai_task_assistant.main:app --reload
```

The health endpoint is available at `http://127.0.0.1:8000/health`.

## Run tests

```bash
pytest
```

## Continuous integration

GitHub Actions runs the complete pytest suite on every push and pull request using Ubuntu and Python 3.12. The workflow installs the project with:

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

Tests mock OpenAI API calls, so CI does not require an `OPENAI_API_KEY` or make live OpenAI requests. After pushing the workflow to GitHub, view its status in the repository's **Actions** tab or on the relevant pull request. This repository does not currently run Docker builds in CI.

## Run with Docker

### Prerequisites

- Docker Desktop or Docker Engine
- An OpenAI API key only when using `POST /chat`; it is not needed for `/health` or Task CRUD.

Build the image:

```bash
docker build -t ai-task-assistant .
```

Run the API without an OpenAI API key to use `/health` and Task CRUD:

```bash
docker run --rm --name ai-task-assistant -p 8000:8000 ai-task-assistant
```

To enable chat, pass secrets only at runtime:

```bash
docker run --rm --name ai-task-assistant -p 8000:8000 \
  -e OPENAI_API_KEY="your-api-key" \
  -e OPENAI_MODEL="gpt-4.1-mini" \
  ai-task-assistant
```

The image does not include `.env` or API keys. You may use `--env-file .env` locally, but do not commit a real `.env` file.

### Persist SQLite data

The default database URL is `sqlite:///./ai_task_assistant.db`, which stores data in the container layer. Use a named Docker volume and an absolute SQLite URL for persistence:

```bash
docker volume create ai-task-assistant-data

docker run --rm --name ai-task-assistant -p 8000:8000 \
  -v ai-task-assistant-data:/data \
  -e DATABASE_URL="sqlite:////data/ai_task_assistant.db" \
  ai-task-assistant
```

The container runs as a non-root user and includes a writable `/data` directory for this volume. A bind mount is also possible when the mounted host directory is writable by the container user.

### Verify health and stop the container

Verify the health endpoint after startup:

```bash
curl http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok"}
```

The Docker image also has a Python-based `HEALTHCHECK`; it does not require `curl` inside the image. To stop a container started without `--rm`:

```bash
docker stop ai-task-assistant
docker rm ai-task-assistant
```

Conversation memory is in-memory and is lost when the application restarts. Authentication is not implemented; this project is a portfolio demonstration and is not production-secure.
