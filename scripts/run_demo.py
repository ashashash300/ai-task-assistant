"""Start a temporary, loopback-only deterministic browser demo.

Run with the repository virtual environment:
``PYTHONPATH=src .venv/bin/python scripts/run_demo.py``.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEMO_TEMP_PREFIX = "ai-task-assistant-demo-"
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8001
FRONTEND_PORT = 5173
DEVELOPMENT_DATABASE_PATH = (REPOSITORY_ROOT / "ai_task_assistant.db").resolve()
PLACEHOLDER_OPENAI_KEY = "demo-placeholder-openai-key"
PLACEHOLDER_GEMINI_KEY = "demo-placeholder-gemini-key"
DEMO_PASSWORD_LENGTH = 24


class DemoLaunchError(RuntimeError):
    """Raised when the demo cannot safely start."""


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path}"


def assert_safe_demo_database_path(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_absolute() or resolved == DEVELOPMENT_DATABASE_PATH:
        raise DemoLaunchError("Refusing to use the development database for the demo")
    if not resolved.parent.name.startswith(DEMO_TEMP_PREFIX):
        raise DemoLaunchError("Demo database must be inside an owned temporary directory")
    return resolved


def cleanup_demo_directory(path: Path) -> None:
    """Remove only a directory created by this launcher."""

    resolved = path.resolve()
    if resolved.name.startswith(DEMO_TEMP_PREFIX):
        shutil.rmtree(resolved, ignore_errors=False)


def assert_port_available(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((BACKEND_HOST, port))
        except OSError as error:
            raise DemoLaunchError(
                f"Loopback port {port} is already in use; refusing to use an existing service"
            ) from error


def wait_for(url: str, label: str, *, timeout_seconds: float = 25.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return
        except (URLError, TimeoutError):
            pass
        time.sleep(0.2)
    raise DemoLaunchError(f"{label} did not become ready")


def _request_json(opener, url: str, *, method: str, headers: dict[str, str], body: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    encoded_body = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(url, data=encoded_body, method=method, headers=headers)
    with opener.open(request, timeout=5.0) as response:
        payload = response.read()
        return response.status, json.loads(payload) if payload else {}


def create_demo_account(*, backend_url: str, origin: str, email: str, password: str) -> None:
    """Create one temporary account through the real protected auth route."""

    cookies = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookies))
    csrf_status, csrf_payload = _request_json(
        opener,
        f"{backend_url}/auth/csrf",
        method="GET",
        headers={"Origin": origin, "Accept": "application/json"},
    )
    if csrf_status != 200 or not isinstance(csrf_payload.get("csrf_token"), str):
        raise DemoLaunchError("Unable to initialize demo CSRF protection")
    register_status, _ = _request_json(
        opener,
        f"{backend_url}/auth/register",
        method="POST",
        headers={
            "Origin": origin,
            "X-CSRF-Token": csrf_payload["csrf_token"],
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        body={"email": email, "password": password},
    )
    if register_status != 201:
        raise DemoLaunchError("Unable to create a temporary demo account")


def demo_credentials(label: str) -> tuple[str, str]:
    suffix = secrets.token_hex(4)
    return f"demo-{label}-{suffix}@local.test", secrets.token_urlsafe(DEMO_PASSWORD_LENGTH)


def start_owned_process(command: list[str], *, environment: dict[str, str], cwd: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        start_new_session=True,
    )


def stop_owned_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def main() -> None:
    temporary_directory = Path(tempfile.mkdtemp(prefix=DEMO_TEMP_PREFIX))
    backend: subprocess.Popen[bytes] | None = None
    frontend: subprocess.Popen[bytes] | None = None
    try:
        database_path = assert_safe_demo_database_path(temporary_directory / "demo.sqlite3")
        assert_port_available(BACKEND_PORT)
        assert_port_available(FRONTEND_PORT)

        vite_binary = REPOSITORY_ROOT / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
        if not vite_binary.is_file():
            raise DemoLaunchError("Frontend dependencies are missing; run npm install in frontend first")

        origin = f"http://{BACKEND_HOST}:{FRONTEND_PORT}"
        instance_id = str(uuid.uuid4())
        environment = {
            **os.environ,
            "AI_TASK_ASSISTANT_DISABLE_DOTENV": "1",
            "AI_TASK_ASSISTANT_DEMO": "1",
            "AI_TASK_ASSISTANT_DEMO_INSTANCE_ID": instance_id,
            "AI_TASK_ASSISTANT_DEMO_TEMP_DIR": str(temporary_directory),
            "AI_TASK_ASSISTANT_DEMO_DATABASE_PATH": str(database_path),
            "AI_TASK_ASSISTANT_DEMO_BACKEND_PORT": str(BACKEND_PORT),
            "AI_TASK_ASSISTANT_DEMO_FRONTEND_PORT": str(FRONTEND_PORT),
            "DATABASE_URL": sqlite_url(database_path),
            "AI_PROVIDER": "openai",
            "OPENAI_API_KEY": PLACEHOLDER_OPENAI_KEY,
            "GEMINI_API_KEY": PLACEHOLDER_GEMINI_KEY,
            "SESSION_COOKIE_SECURE": "false",
            "AUTH_ALLOWED_ORIGINS": origin,
            "CHAT_DAILY_QUOTA": "5",
            "PYTHONPATH": "src",
        }
        backend = start_owned_process(
            [sys.executable, "scripts/demo_server.py"], environment=environment, cwd=REPOSITORY_ROOT
        )
        wait_for(f"http://{BACKEND_HOST}:{BACKEND_PORT}/health", "Demo backend")

        frontend = start_owned_process(
            ["node", str(vite_binary), "--host", BACKEND_HOST, "--port", str(FRONTEND_PORT)],
            environment={**environment, "VITE_API_PROXY_TARGET": f"http://{BACKEND_HOST}:{BACKEND_PORT}"},
            cwd=REPOSITORY_ROOT / "frontend",
        )
        wait_for(origin, "Demo frontend")

        account_a = demo_credentials("a")
        account_b = demo_credentials("b")
        backend_url = f"http://{BACKEND_HOST}:{BACKEND_PORT}"
        create_demo_account(backend_url=backend_url, origin=origin, email=account_a[0], password=account_a[1])
        create_demo_account(backend_url=backend_url, origin=origin, email=account_b[0], password=account_b[1])

        print("\nAI Task Assistant deterministic demo is ready")
        print(f"Open: {origin}")
        print("\nTemporary credentials (deleted with the demo database):")
        print(f"  Account A email: {account_a[0]}")
        print(f"  Account A password: {account_a[1]}")
        print(f"  Account B email: {account_b[0]}")
        print(f"  Account B password: {account_b[1]}")
        print("\nSupported chat commands:")
        print("  建立面試準備任務")
        print("  完成剛剛的任務")
        print("\nPress Ctrl+C to stop and remove all temporary demo data.")

        interrupted = False

        def request_shutdown(_signum, _frame) -> None:
            nonlocal interrupted
            interrupted = True

        signal.signal(signal.SIGINT, request_shutdown)
        signal.signal(signal.SIGTERM, request_shutdown)
        while not interrupted:
            if backend.poll() is not None or frontend.poll() is not None:
                raise DemoLaunchError("An owned demo process stopped unexpectedly")
            time.sleep(0.2)
    except (DemoLaunchError, URLError, OSError, json.JSONDecodeError) as error:
        print(f"demo launcher: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    finally:
        stop_owned_process(frontend)
        stop_owned_process(backend)
        cleanup_demo_directory(temporary_directory)


if __name__ == "__main__":
    main()
