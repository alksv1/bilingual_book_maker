"""Cross-layer web contracts, with isolated storage and no provider network calls."""

import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from book_maker import provider_loader
from server import config
from server.main import app
from server.middleware.auth import SessionManager
from server.routers import auth, settings, tasks
from server.services.runner import TaskRunner
from server.services.settings import SettingsService

REPO = Path(__file__).resolve().parents[1]
PASSWORD = "synthetic-web-integration-password"
SAVED_KEY = "sk-synthetic-saved-integration-only"


@pytest.fixture
def web(tmp_path, monkeypatch):
    """Real app lifespans, fresh runner/service, and every mutable path under tmp."""
    for name, value in {
        "DATA_DIR": tmp_path,
        "CONFIG_DIR": tmp_path / "config",
        "UPLOADS_DIR": tmp_path / "uploads",
        "OUTPUT_DIR": tmp_path / "output",
        "LOGS_DIR": tmp_path / "logs",
        "DB_PATH": tmp_path / "tasks.db",
        "GENERAL_CONFIG_PATH": tmp_path / "config/general.json",
        "PROVIDERS_CONFIG_PATH": tmp_path / "config/bbm_providers.json",
        "AUTH_PASSWORD": "",
    }.items():
        monkeypatch.setattr(config, name, value)
    home = tmp_path / "home"
    home.mkdir()
    for name in tuple(os.environ):
        if name.startswith("BBM_") or name.endswith(
            ("_API_KEY", "_API_BASE", "_BASE_URL")
        ):
            monkeypatch.delenv(name)
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "APPDATA"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("PYTHONPATH", "")
    monkeypatch.setattr(
        provider_loader, "GLOBAL_CONFIG_PATH", home / ".bbm/providers.json"
    )
    service = SettingsService(config_dir=config.CONFIG_DIR, db_path=config.DB_PATH)
    runner = TaskRunner()
    # Both router and module-level fetch_task must use the same fresh runner.
    import server.services.runner as runner_module

    monkeypatch.setattr(runner_module, "runner", runner)
    monkeypatch.setattr(tasks, "runner", runner)
    monkeypatch.setattr(settings, "settings_service", service)
    monkeypatch.setattr(auth, "rate_limiter", auth.LoginRateLimiter())

    def blocked(*args, **kwargs):
        raise AssertionError("Integration test attempted network access")

    async def blocked_async(*args, **kwargs):
        blocked()

    real_connect = socket.socket.connect

    def local_only(sock, address):
        # Windows implements asyncio self-pipes with a loopback socketpair.
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return real_connect(sock, address)
        blocked()

    monkeypatch.setattr(socket.socket, "connect", local_only)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)
    with TestClient(app) as client:
        yield client, runner, service
    assert not runner._active_processes
    assert not runner._executions
    assert not runner._workers


def create(client, *, headers=None, data=None):
    response = client.post(
        "/api/tasks",
        headers=headers,
        data=data or {},
        files={
            "files": (
                "chapter.txt",
                b"First sentence.\nSecond sentence.\n",
                "text/plain",
            )
        },
    )
    assert response.status_code == 201, response.text
    return response.json()[0]["id"]


def terminal(client, task_id, *, headers=None, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get("/api/tasks", headers=headers)
        assert response.status_code == 200, response.text
        item = next(item for item in response.json()["items"] if item["id"] == task_id)
        if item["status"] in {"completed", "failed", "cancelled"}:
            return item
        time.sleep(0.05)
    pytest.fail(f"Task {task_id} did not terminate within {timeout}s")


def sse(client, task_id, headers=None):
    """ASGI transport plus wait_for bounds even a broken never-ending stream."""

    async def read():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as http:
            return await asyncio.wait_for(
                http.get(f"/api/tasks/{task_id}/logs/stream", headers=headers),
                timeout=5,
            )

    return client.portal.call(read)


@pytest.mark.parametrize("ambient", [None, "", "sk-synthetic-ambient-integration-only"])
def test_settings_api_reaches_real_child_environment(web, monkeypatch, ambient):
    client, runner, service = web
    response = client.put(
        "/api/settings/providers/offline",
        json={
            "base_url": "https://offline.invalid/v1",
            "api_key": SAVED_KEY,
            "default_models": ["offline-model"],
            "set_as_active": True,
        },
    )
    assert response.status_code == 200, response.text
    assert SAVED_KEY not in response.text
    assert (
        client.put(
            "/api/settings/general", json={"default_target_lang": "fr"}
        ).status_code
        == 200
    )
    document = json.loads(config.PROVIDERS_CONFIG_PATH.read_text(encoding="utf-8"))
    env_key = document["providers"]["offline"]["env_key"]
    if ambient is not None:
        monkeypatch.setenv(env_key, ambient)
    capture = config.DATA_DIR / "child-environment.json"
    real_spawn = asyncio.create_subprocess_exec
    observed = []

    async def spawn(*cmd, **kwargs):
        observed.append((cmd, kwargs["cwd"]))
        # Capture from inside a real child, not merely the parent's spawn kwargs.
        source = Path(cmd[cmd.index("--book_name") + 1])
        output = source.with_name(source.stem + "_bilingual.txt")
        script = (
            "import json,os,pathlib,sys; "
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({'key':os.environ.get(sys.argv[2]),"
            "'cwd':os.getcwd()})); pathlib.Path(sys.argv[3]).write_text('translated'); print('[1/1] done')"
        )
        return await real_spawn(
            sys.executable, "-c", script, str(capture), env_key, str(output), **kwargs
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task_id = create(client)
    result = terminal(client, task_id)
    assert result["status"] == "completed", result
    assert (result["progress_current"], result["progress_total"]) == (1, 1)
    child = json.loads(capture.read_text())
    assert child["key"] == (ambient or SAVED_KEY)
    assert Path(child["cwd"]).resolve() == config.CONFIG_DIR.resolve()
    cmd, cwd = observed[0]
    assert cmd[:4] == (sys.executable, "-m", "book_maker.cli", "--book_name")
    assert cmd[cmd.index("--provider") + 1] == "offline"
    assert cmd[cmd.index("--language") + 1] == "fr"
    assert SAVED_KEY not in " ".join(cmd)
    # A newly constructed service reads persisted keys, rather than process cache.
    fresh = SettingsService(config_dir=service.config_dir, db_path=service.db_path)
    assert client.portal.call(fresh.get_provider_environment)[env_key] == SAVED_KEY
    assert os.environ.get(env_key) == ambient
    public = client.get("/api/settings/providers")
    assert public.json()["active_provider"] == "offline"
    assert SAVED_KEY not in public.text
    stream = sse(client, task_id)
    assert stream.status_code == 200
    assert SAVED_KEY not in stream.text


# Keep this inventory explicit: missing auth on even downloads or SSE is a leak.
ENDPOINTS = [
    ("GET", "/api/tasks"),
    ("POST", "/api/tasks"),
    ("POST", "/api/tasks/{id}/cancel"),
    ("DELETE", "/api/tasks/{id}"),
    ("GET", "/api/tasks/{id}/logs/stream"),
    ("GET", "/api/tasks/{id}/download"),
    ("GET", "/api/settings/general"),
    ("PUT", "/api/settings/general"),
    ("GET", "/api/settings/providers"),
    ("PUT", "/api/settings/providers/check"),
    ("POST", "/api/settings/providers/check/test"),
]


@pytest.mark.parametrize(
    "credential",
    ["missing", "bad-bearer", "bad-cookie", "expired-cookie", "bearer", "cookie"],
)
def test_auth_matrix_all_task_and_settings_routes(web, monkeypatch, credential):
    client, runner, _ = web
    monkeypatch.setattr(config, "AUTH_PASSWORD", PASSWORD)

    # Pending uploads stay pending; cancel/delete still exercise the real runner API.
    async def do_not_enqueue(task_id):
        pass

    monkeypatch.setattr(runner, "enqueue", do_not_enqueue)
    bearer = {"Authorization": f"Bearer {PASSWORD}"}
    assert (
        client.put(
            "/api/settings/providers/check",
            headers=bearer,
            json={"base_url": "https://offline.invalid/v1", "api_key": SAVED_KEY},
        ).status_code
        == 200
    )
    probes = []

    async def probe(transport, request):
        probes.append(request)
        assert request.url.host == "offline.invalid"
        return httpx.Response(200, json={"data": []}, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", probe)
    completed = create(client, headers=bearer)
    output = config.OUTPUT_DIR / f"{completed}_bilingual.txt"
    output.write_text("translated fixture", encoding="utf-8")
    (config.LOGS_DIR / f"{completed}.log").write_text(
        "[12:34:56] terminal fixture\n", encoding="utf-8"
    )

    async def complete():
        import aiosqlite

        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                "UPDATE tasks SET status='completed' WHERE id=?", (completed,)
            )
            await db.commit()

    client.portal.call(complete)
    cancel_id = create(client, headers=bearer)
    delete_id = create(client, headers=bearer)
    headers = {}
    if credential == "bearer":
        headers = bearer
    elif credential == "cookie":
        login = client.post("/api/auth/login", json={"password": PASSWORD})
        assert login.status_code == 200
        assert "httponly" in login.headers["set-cookie"].lower()
        headers = {
            "Cookie": f"{config.SESSION_COOKIE_NAME}={client.cookies[config.SESSION_COOKIE_NAME]}"
        }
        client.cookies.clear()
    elif credential == "bad-bearer":
        headers = {"Authorization": "Bearer synthetic-wrong"}
    elif credential in {"bad-cookie", "expired-cookie"}:
        token = (
            SessionManager.create_session_token(PASSWORD, max_age=-1)
            if credential == "expired-cookie"
            else "invalid.signature"
        )
        headers = {"Cookie": f"{config.SESSION_COOKIE_NAME}={token}"}
    allowed = credential in {"bearer", "cookie"}
    for method, path in ENDPOINTS:
        task_id = (
            cancel_id
            if path.endswith("/cancel")
            else delete_id if method == "DELETE" else completed
        )
        url = path.replace("{id}", task_id)
        kwargs = {"headers": headers}
        if method == "POST" and path == "/api/tasks":
            kwargs["files"] = {"files": ("more.txt", b"hello")}
        elif method in {"PUT", "POST"}:
            kwargs["json"] = {}
        response = (
            sse(client, task_id, headers)
            if path.endswith("/logs/stream")
            else client.request(method, url, **kwargs)
        )
        if not allowed:
            assert response.status_code == 401, (method, url, response.text)
            assert response.json()["code"] == "UNAUTHORIZED"
        else:
            expected = (
                201
                if path == "/api/tasks" and method == "POST"
                else (204 if method == "DELETE" else 200)
            )
            assert response.status_code == expected, (method, url, response.text)
            if path.endswith("/logs/stream"):
                assert response.headers["content-type"].startswith("text/event-stream")
                assert (
                    json.loads(response.text.removeprefix("data: ").strip())["line"]
                    == "terminal fixture"
                )
            if path.endswith("/download"):
                assert response.text == "translated fixture"
                assert (
                    "chapter_bilingual.txt" in response.headers["content-disposition"]
                )
    if not allowed:
        # Rejected mutations did not cancel, delete, create, or save settings.
        listing = client.get("/api/tasks", headers=bearer).json()
        assert listing["total"] == 3
        assert not config.GENERAL_CONFIG_PATH.exists()
        assert not probes
        assert (
            json.loads(config.PROVIDERS_CONFIG_PATH.read_text(encoding="utf-8"))[
                "providers"
            ]["check"]["base_url"]
            == "https://offline.invalid/v1"
        )
    else:
        assert len(probes) == 1


def test_real_offline_cli_lifecycle_through_web(web, monkeypatch):
    client, runner, _ = web
    # The startup hook swaps only the provider. CLI parsing, loaders, subprocess,
    # persistence, output relocation and terminal SSE are production code.
    hooks = config.DATA_DIR / "hooks"
    hooks.mkdir()
    hermetic = REPO / "tests/hermetic/sitecustomize.py"
    (hooks / "sitecustomize.py").write_text(
        "import socket, runpy\n"
        "def blocked(*args, **kwargs):\n    raise RuntimeError('offline test forbids network')\n"
        "socket.socket.connect = blocked\nsocket.create_connection = blocked\n"
        f"runpy.run_path({str(hermetic)!r})\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(hooks))
    response = client.put(
        "/api/settings/providers/offline",
        json={
            "base_url": "https://offline.invalid/v1",
            "api_key": SAVED_KEY,
            "default_models": ["offline-model"],
            "set_as_active": True,
        },
    )
    assert response.status_code == 200, response.text
    assert (
        client.put(
            "/api/settings/general", json={"default_target_lang": "fr"}
        ).status_code
        == 200
    )
    task_id = create(client, data={"prompt": "Translate faithfully: {text}"})
    result = terminal(client, task_id, timeout=45)
    stream = sse(client, task_id)
    assert result["status"] == "completed", (result, stream.text)
    assert result["model_provider"] == "offline"
    assert result["target_language"] == "fr"
    assert result["prompt_template"] == "Translate faithfully: {text}"
    assert stream.status_code == 200
    messages = [
        json.loads(line[6:])
        for line in stream.text.splitlines()
        if line.startswith("data: ")
    ]
    assert any("offline model list" in message["line"] for message in messages)
    assert messages[-1]["line"] == "[System] Task completed."
    assert SAVED_KEY not in stream.text
    download = client.get(f"/api/tasks/{task_id}/download")
    assert download.status_code == 200
    assert "[offline]" in download.text
    assert "First sentence." in download.text
    source = config.UPLOADS_DIR / f"{task_id}.txt"
    assert not source.with_name(source.stem + "_bilingual.txt").exists()
    assert client.delete(f"/api/tasks/{task_id}?delete_file=true").status_code == 204
    assert client.get("/api/tasks").json()["total"] == 0
    assert not source.exists()
    assert not (config.OUTPUT_DIR / f"{task_id}_bilingual.txt").exists()
    assert not (config.LOGS_DIR / f"{task_id}.log").exists()
    assert client.get(f"/api/tasks/{task_id}/download").status_code == 404
    assert sse(client, task_id).status_code == 404


def test_controlled_pdf_child_is_relocated_and_downloaded_as_epub(web, monkeypatch):
    """Exercise PDF output naming without importing an OCR/PDF implementation."""
    client, _, _ = web
    real_spawn = asyncio.create_subprocess_exec
    epub_bytes = b"PK\x03\x04synthetic epub output"
    commands = []

    async def spawn(*cmd, **kwargs):
        commands.append(cmd)
        source = Path(cmd[cmd.index("--book_name") + 1])
        assert source.suffix == ".pdf"
        expected = source.with_name(source.stem + "_bilingual.epub")
        return await real_spawn(
            sys.executable,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(bytes.fromhex(sys.argv[2])); print('[1/1] PDF converted')",
            str(expected),
            epub_bytes.hex(),
            **kwargs,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    response = client.post(
        "/api/tasks",
        files={
            "files": (
                "source.scan.pdf",
                b"%PDF-1.4\nsynthetic source, not parsed",
                "application/pdf",
            ),
        },
    )
    assert response.status_code == 201, response.text
    task_id = response.json()[0]["id"]
    result = terminal(client, task_id)
    assert result["status"] == "completed", result
    assert commands[0][:3] == (sys.executable, "-m", "book_maker.cli")
    download = client.get(f"/api/tasks/{task_id}/download")
    assert download.status_code == 200
    assert download.content == epub_bytes
    assert download.headers["content-type"] == "application/epub+zip"
    assert "source.scan_bilingual.epub" in download.headers["content-disposition"]
    assert (config.OUTPUT_DIR / f"{task_id}_bilingual.epub").read_bytes() == epub_bytes
    assert not (config.UPLOADS_DIR / f"{task_id}_bilingual.epub").exists()
    assert "[System] Task completed." in sse(client, task_id).text
