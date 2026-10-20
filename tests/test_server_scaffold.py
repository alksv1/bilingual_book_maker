"""Tests for Web server scaffolding, SQLite database, and API contracts (Issue #1)."""

import pytest
import aiosqlite
import httpx
from pathlib import Path
from fastapi.testclient import TestClient

from server.main import app
from server import config
from server.database import init_db
from server.routers import settings as settings_router
from server.services.settings import SettingsService
from server.schemas import (
    AuthStatusResponse,
    LoginRequest,
    GeneralSettings,
    ProvidersListResponse,
    TaskListResponse,
)


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    """Scaffold tests must not contact any external HTTP endpoint."""
    async def blocked_async(*args, **kwargs):
        pytest.fail("Unexpected external async HTTP request")

    def blocked_sync(*args, **kwargs):
        pytest.fail("Unexpected external sync HTTP request")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked_sync)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """Create a TestClient with temporary data directory."""
    temp_data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", temp_data)
    monkeypatch.setattr(config, "CONFIG_DIR", temp_data / "config")
    monkeypatch.setattr(config, "UPLOADS_DIR", temp_data / "uploads")
    monkeypatch.setattr(config, "OUTPUT_DIR", temp_data / "output")
    monkeypatch.setattr(config, "LOGS_DIR", temp_data / "logs")
    monkeypatch.setattr(config, "DB_PATH", temp_data / "bbm_tasks.db")
    monkeypatch.setattr(settings_router, "settings_service", SettingsService(
        config_dir=temp_data / "config", db_path=temp_data / "bbm_tasks.db",
    ))

    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.asyncio
async def test_database_initialization(tmp_path: Path):
    """Test that SQLite database tables (tasks, app_settings) are properly created with indices."""
    db_file = tmp_path / "test_tasks.db"
    await init_db(db_file)

    assert db_file.exists(), "Database file should exist after init_db"

    async with aiosqlite.connect(db_file) as db:
        # Check tasks table
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks';"
        )
        row = await cursor.fetchone()
        assert row is not None, "tasks table must exist"

        # Check tasks table columns
        cursor = await db.execute("PRAGMA table_info(tasks);")
        columns = {col[1]: col[2] for col in await cursor.fetchall()}
        expected_cols = [
            "id",
            "filename",
            "file_path",
            "output_path",
            "file_type",
            "status",
            "progress_current",
            "progress_total",
            "model_provider",
            "target_language",
            "prompt_template",
            "error_message",
            "log_path",
            "created_at",
            "updated_at",
        ]
        for col_name in expected_cols:
            assert col_name in columns, f"Column {col_name} must exist in tasks table"

        # Check app_settings table
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='app_settings';"
        )
        row = await cursor.fetchone()
        assert row is not None, "app_settings table must exist"

        cursor = await db.execute("PRAGMA table_info(app_settings);")
        columns = {col[1]: col[2] for col in await cursor.fetchall()}
        assert "key" in columns
        assert "value" in columns
        assert "updated_at" in columns


def test_health_check_endpoint(client: TestClient):
    """Test /api/health endpoint returns 200 OK with correct payload."""
    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["version"] == "1.0.0"
    assert "protected_mode" in data


def test_openapi_docs_generation(client: TestClient):
    """Test that OpenAPI schema is properly generated and contains all contract paths."""
    res = client.get("/openapi.json")
    assert res.status_code == 200
    openapi = res.json()
    paths = openapi.get("paths", {})

    expected_endpoints = [
        "/api/auth/status",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/tasks",
        "/api/tasks/{task_id}/cancel",
        "/api/tasks/{task_id}",
        "/api/tasks/{task_id}/logs/stream",
        "/api/tasks/{task_id}/download",
        "/api/settings/general",
        "/api/settings/providers",
        "/api/settings/providers/{provider_name}",
        "/api/settings/providers/{provider_name}/test",
        "/api/health",
    ]
    for ep in expected_endpoints:
        assert ep in paths, f"OpenAPI schema must declare endpoint {ep}"


def test_auth_router_stubs(client: TestClient):
    """Test auth endpoints contracts."""
    # GET /api/auth/status
    res = client.get("/api/auth/status")
    assert res.status_code == 200
    status_data = AuthStatusResponse(**res.json())
    assert isinstance(status_data.protected_mode, bool)
    assert isinstance(status_data.authenticated, bool)

    # POST /api/auth/login
    res = client.post("/api/auth/login", json={"password": "any"})
    assert res.status_code == 200
    assert res.json()["success"] is True

    # POST /api/auth/logout
    res = client.post("/api/auth/logout")
    assert res.status_code == 200
    assert res.json()["success"] is True


def test_tasks_router_stubs(client: TestClient, monkeypatch):
    """Test persistent task endpoint contracts without external translation calls."""
    from unittest.mock import AsyncMock
    from server.routers.tasks import runner

    monkeypatch.setattr(runner, "enqueue", AsyncMock())
    # POST /api/tasks (multipart upload)
    files = [
        ("files", ("test1.epub", b"dummy content 1", "application/epub+zip")),
        ("files", ("test2.pdf", b"dummy content 2", "application/pdf")),
    ]
    res = client.post(
        "/api/tasks",
        files=files,
        data={"target_lang": "zh-hans", "provider": "openai"},
    )
    assert res.status_code == 201
    tasks = res.json()
    assert isinstance(tasks, list)
    assert len(tasks) == 2
    assert tasks[0]["filename"] == "test1.epub"
    assert tasks[0]["status"] == "pending"

    # GET /api/tasks
    res = client.get("/api/tasks?status=all&limit=20&offset=0")
    assert res.status_code == 200
    task_list = TaskListResponse(**res.json())
    assert task_list.total == 2
    assert {task.id for task in task_list.items} == {task["id"] for task in tasks}

    # POST /api/tasks/{id}/cancel
    task_id = tasks[0]["id"]
    res = client.post(f"/api/tasks/{task_id}/cancel")
    assert res.status_code == 200
    assert res.json()["status"] == "cancelled"

    # GET /api/tasks/{id}/logs/stream
    res = client.get(f"/api/tasks/{task_id}/logs/stream")
    assert res.status_code == 200
    assert "text/event-stream" in res.headers["content-type"]

    res = client.delete(f"/api/tasks/{task_id}?delete_file=true")
    assert res.status_code == 204

    # Missing tasks return 404.
    res = client.get(f"/api/tasks/{task_id}/download")
    assert res.status_code == 404
    err = res.json()
    assert "detail" in err


def test_settings_router_contracts(client: TestClient, monkeypatch):
    """Exercise real settings persistence with a mocked upstream models request."""
    # GET /api/settings/general
    res = client.get("/api/settings/general")
    assert res.status_code == 200
    general = GeneralSettings(**res.json())
    assert general.default_target_lang == "zh-hans"

    # PUT /api/settings/general
    res = client.put(
        "/api/settings/general",
        json={"default_target_lang": "en", "concurrency_limit": 2},
    )
    assert res.status_code == 200
    updated = GeneralSettings(**res.json())
    assert updated.default_target_lang == "en"
    assert updated.concurrency_limit == 2

    # GET /api/settings/providers
    res = client.get("/api/settings/providers")
    assert res.status_code == 200
    providers = ProvidersListResponse(**res.json())
    assert "openai" in providers.providers
    assert providers.providers["openai"].has_api_key is False
    assert providers.providers["openai"].masked_api_key is None

    # PUT /api/settings/providers/{name}
    res = client.put(
        "/api/settings/providers/openai",
        json={"base_url": "https://custom.api.com", "api_key": "sk-123456789"},
    )
    assert res.status_code == 200
    p_info = res.json()
    assert p_info["base_url"] == "https://custom.api.com"
    assert p_info["has_api_key"] is True

    # POST /api/settings/providers/{name}/test never reaches the network.
    requests = []

    async def models_response(transport, request):
        requests.append(request)
        return httpx.Response(200, json={"data": []}, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", models_response)
    res = client.post(
        "/api/settings/providers/openai/test",
        json={"base_url": "https://api.openai.com/v1"},
    )
    assert res.status_code == 200
    assert res.json()["success"] is True
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/v1/models"
    assert requests[0].headers["authorization"] == "Bearer sk-123456789"


def test_error_response_format(client: TestClient):
    """Test that errors return uniform ErrorResponse JSON format."""
    # Non-existent endpoint -> 404
    res = client.get("/api/non-existent-endpoint")
    assert res.status_code == 404
    err = res.json()
    assert "detail" in err
    assert "code" in err
