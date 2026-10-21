"""Real settings persistence and connectivity tests; never contact a provider."""

import asyncio
import json
import os
from pathlib import Path

import aiosqlite
import httpx
import pytest
from fastapi.testclient import TestClient

from book_maker.provider_loader import validate_provider
from server import config
from server.database import init_db
from server.main import app
from server.routers import settings as settings_router
from server.schemas import GeneralSettingsUpdate, ProviderTestRequest, ProviderUpdateRequest
from server.services import settings as settings_module
from server.services.settings import SettingsError, SettingsService, mask_api_key


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    """Fail closed, even if connectivity accidentally falls back to another call."""
    async def blocked_async(*args, **kwargs):
        pytest.fail("Unexpected external async HTTP request")

    def blocked_sync(*args, **kwargs):
        pytest.fail("Unexpected external sync HTTP request")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked_sync)
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "BBM_GOOGLE_GEMINI_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def service(tmp_path):
    return SettingsService(config_dir=tmp_path / "config", db_path=tmp_path / "settings.db")


def fresh(service):
    return SettingsService(config_dir=service.config_dir, db_path=service.db_path)


def write_providers(service, providers):
    service.config_dir.mkdir(parents=True, exist_ok=True)
    (service.config_dir / "bbm_providers.json").write_text(
        json.dumps({"providers": providers}), encoding="utf-8"
    )


@pytest.fixture
def mock_http(monkeypatch):
    requests = []

    def install(status=200, body=None, error=None, headers=None):
        async def handle(transport, request):
            requests.append(request)
            if error is not None:
                raise error("sensitive-upstream-error sk-private-secret", request=request)
            return httpx.Response(
                status, json=body if body is not None else {"data": []},
                headers=headers, request=request,
            )
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
        return requests

    return install


@pytest.fixture
def client(service, monkeypatch, tmp_path):
    for name, value in {
        "DATA_DIR": tmp_path,
        "CONFIG_DIR": service.config_dir,
        "DB_PATH": service.db_path,
        "UPLOADS_DIR": tmp_path / "uploads",
        "OUTPUT_DIR": tmp_path / "output",
        "LOGS_DIR": tmp_path / "logs",
    }.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(settings_router, "settings_service", service)
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize("key,masked", [
    (None, None), ("", None), ("12345", "****"), ("12345678", "****"),
    ("sk-proj-1234567890AbCd", "sk-p****AbCd"),
])
def test_mask_api_key(key, masked):
    assert mask_api_key(key) == masked


@pytest.mark.asyncio
async def test_general_defaults_partial_update_and_restart(service):
    await init_db(service.db_path)
    defaults = await service.get_general_settings()
    assert defaults.model_dump() == {
        "default_target_lang": "zh-hans", "bilingual_style": "default",
        "concurrency_limit": 1, "max_retries": 3,
    }
    await service.update_general_settings(
        GeneralSettingsUpdate(default_target_lang="en", concurrency_limit=2)
    )
    saved = await fresh(service).get_general_settings()
    assert saved.default_target_lang == "en"
    assert saved.concurrency_limit == 2
    assert saved.bilingual_style == "default"
    assert saved.max_retries == 3
    disk = json.loads((service.config_dir / "general.json").read_text(encoding="utf-8"))
    assert all(disk[key] == value for key, value in saved.model_dump().items())
    async with aiosqlite.connect(service.db_path) as db:
        async with db.execute(
            "SELECT value FROM app_settings WHERE key = 'general_settings'"
        ) as cursor:
            row = await cursor.fetchone()
    assert row is not None
    assert json.loads(row[0]) == saved.model_dump()


@pytest.mark.asyncio
async def test_json_is_primary_and_database_is_fallback(service):
    await init_db(service.db_path)
    await service.update_general_settings(GeneralSettingsUpdate(max_retries=7))
    path = service.config_dir / "general.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["max_retries"] = 9
    path.write_text(json.dumps(data), encoding="utf-8")
    assert (await fresh(service).get_general_settings()).max_retries == 9
    path.unlink()
    assert (await fresh(service).get_general_settings()).max_retries == 7


@pytest.mark.asyncio
async def test_provider_defaults_have_no_fictional_key(service, monkeypatch):
    write_providers(service, {"openai": {"api_style": "openai", "env_key": "OPENAI_API_KEY"}})
    response = await service.get_providers()
    assert response.providers["openai"].has_api_key is False
    assert response.providers["openai"].masked_api_key is None
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-secret-12345678")
    response = await service.get_providers()
    assert response.providers["openai"].has_api_key is True
    assert response.providers["openai"].masked_api_key == "sk-e****5678"
    assert "sk-env-secret-12345678" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_provider_key_active_and_general_persist_across_instances(service):
    await init_db(service.db_path)
    secret = "sk-saved-secret-12345678"
    await service.update_provider("custom", ProviderUpdateRequest(
        base_url="https://custom.example/v1", api_key=secret,
        default_models=["model-a"], set_as_active=True,
    ))
    await fresh(service).update_general_settings(GeneralSettingsUpdate(max_retries=0))
    restarted = fresh(service)
    assert (await restarted.get_saved_keys())["custom"] == secret
    assert await restarted.get_active_provider() == "custom"
    assert (await restarted.get_general_settings()).max_retries == 0
    listing = await restarted.get_providers()
    assert listing.active_provider == "custom"
    assert listing.providers["custom"].has_api_key
    assert secret not in listing.model_dump_json()
    providers = json.loads((service.config_dir / "bbm_providers.json").read_text(encoding="utf-8"))
    assert secret not in json.dumps(providers)
    for name, entry in providers["providers"].items():
        validate_provider(name, entry)
        assert "api_key" not in entry


@pytest.mark.asyncio
@pytest.mark.parametrize("key_update", [{}, {"api_key": None}, {"api_key": ""}, {"api_key": "   "}])
async def test_missing_and_empty_key_preserve_saved_key(service, key_update):
    secret = "sk-preserved-secret-12345678"
    await service.update_provider("custom", ProviderUpdateRequest(
        base_url="https://custom.example/v1", default_models=["model-a"], api_key=secret,
    ))
    result = await fresh(service).update_provider("custom", ProviderUpdateRequest(
        base_url="https://changed.example/v1", **key_update,
    ))
    assert result.has_api_key
    assert result.masked_api_key == mask_api_key(secret)
    assert (await fresh(service).get_saved_keys())["custom"] == secret


@pytest.mark.asyncio
async def test_saved_keys_get_distinct_runner_environment_names(service):
    write_providers(service, {
        name: {"api_style": "openai", "base_url": "https://custom.example/v1",
               "env_key": "OPENAI_API_KEY", "default_models": ["model-a"]}
        for name in ("one", "two")
    })
    for name in ("one", "two"):
        await service.update_provider(name, ProviderUpdateRequest(api_key=f"secret-{name}"))
    providers = json.loads((service.config_dir / "bbm_providers.json").read_text(encoding="utf-8"))["providers"]
    one, two = providers["one"]["env_key"], providers["two"]["env_key"]
    assert one != two
    assert one != "OPENAI_API_KEY"
    assert two != "OPENAI_API_KEY"
    environment = await fresh(service).get_provider_environment()
    assert environment[one] == "secret-one"
    assert environment[two] == "secret-two"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ambient,saved,expected",
    [
        ("ambient-secret", "saved-secret", "ambient-secret"),
        ("ambient-secret", "   ", "ambient-secret"),
        (None, "saved-secret", "saved-secret"),
        ("", "saved-secret", "saved-secret"),
        ("   ", "saved-secret", "saved-secret"),
        (None, "   ", ""),
        ("   ", "", ""),
    ],
)
async def test_provider_credential_priority(
    service, monkeypatch, mock_http, ambient, saved, expected
):
    env_key = "BBM_TEST_PROVIDER_API_KEY"
    write_providers(
        service,
        {
            "custom": {
                "api_style": "openai",
                "base_url": "https://custom.example/v1",
                "env_key": env_key,
            }
        },
    )
    service.keys_config_path.write_text(json.dumps({"custom": saved}), encoding="utf-8")
    monkeypatch.delenv(env_key, raising=False)
    if ambient is not None:
        monkeypatch.setenv(env_key, ambient)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-vendor-secret")
    before = dict(os.environ)
    restarted = fresh(service)
    listing = await restarted.get_providers()
    assert listing.providers["custom"].has_api_key is bool(expected)
    assert listing.providers["custom"].masked_api_key == mask_api_key(expected)
    environment = await restarted.get_provider_environment()
    assert environment == ({env_key: saved.strip()} if saved.strip() else {})
    requests = mock_http()
    assert (await restarted.test_provider("custom", ProviderTestRequest())).success
    assert requests[0].headers.get("authorization") == (
        f"Bearer {expected}" if expected else None
    )
    assert dict(os.environ) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ambient,saved,expected",
    [
        ("ambient-secret", "saved-secret", "ambient-secret"),
        (None, "saved-secret", "saved-secret"),
        ("", "saved-secret", "saved-secret"),
        ("   ", "saved-secret", "saved-secret"),
        (None, "   ", None),
        ("", "   ", ""),
    ],
)
async def test_runner_child_credential_priority(
    service, monkeypatch, tmp_path, ambient, saved, expected
):
    from server.services import runner as runner_module

    env_key = "BBM_TEST_PROVIDER_API_KEY"
    write_providers(service, {"custom": {"api_style": "openai", "env_key": env_key}})
    service.keys_config_path.write_text(json.dumps({"custom": saved}), encoding="utf-8")
    monkeypatch.delenv(env_key, raising=False)
    if ambient is not None:
        monkeypatch.setenv(env_key, ambient)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-vendor-secret")
    for name, value in {
        "DATA_DIR": tmp_path,
        "CONFIG_DIR": service.config_dir,
        "DB_PATH": service.db_path,
        "UPLOADS_DIR": tmp_path / "uploads",
        "OUTPUT_DIR": tmp_path / "output",
        "LOGS_DIR": tmp_path / "logs",
        "STATIC_DIR": tmp_path / "static",
    }.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(runner_module, "settings_service", fresh(service))
    await init_db(service.db_path)
    task_id = "credential-test"
    async with aiosqlite.connect(service.db_path) as db:
        await db.execute(
            "INSERT INTO tasks (id, filename, file_path, file_type, status, log_path) "
            "VALUES (?, ?, ?, 'txt', 'pending', ?)",
            (
                task_id,
                "book.txt",
                str(tmp_path / "book.txt"),
                str(tmp_path / "task.log"),
            ),
        )
        await db.commit()
    captured = []

    async def capture_spawn(*args, **kwargs):
        captured.append(kwargs["env"])
        raise RuntimeError("Test stops before spawning a child")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_spawn)
    before = dict(os.environ)
    runner = runner_module.TaskRunner()
    runner._db_path = service.db_path
    await runner._execute_task(await runner.fetch_task(task_id))
    assert len(captured) == 1
    assert captured[0].get(env_key) == expected
    assert captured[0]["OPENAI_API_KEY"] == "unrelated-vendor-secret"
    assert dict(os.environ) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"bilingual_style": "sideways"}, {"concurrency_limit": 0},
    {"concurrency_limit": -1}, {"max_retries": -1},
])
async def test_invalid_general_values_do_not_replace_saved_settings(service, updates):
    await service.update_general_settings(GeneralSettingsUpdate(max_retries=4))
    path = service.config_dir / "general.json"
    before = path.read_bytes()
    with pytest.raises(SettingsError) as error:
        await service.update_general_settings(GeneralSettingsUpdate(**updates))
    assert error.value.status_code in (400, 422)
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"default_models": []}, {"default_models": [""]}, {"default_models": ["  "]},
    {"base_url": "not-a-url"}, {"base_url": "ftp://custom.example"},
    {"base_url": "https://user:password@custom.example/v1"},
])
async def test_invalid_provider_values_do_not_overwrite(service, updates):
    await service.update_provider("custom", ProviderUpdateRequest(
        base_url="https://custom.example/v1", default_models=["model-a"],
    ))
    path = service.config_dir / "bbm_providers.json"
    before = path.read_bytes()
    with pytest.raises(SettingsError) as error:
        await service.update_provider("custom", ProviderUpdateRequest(**updates))
    assert error.value.status_code in (400, 422)
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("filename,operation", [
    ("general.json", "general"), ("keys.json", "keys"),
    ("bbm_providers.json", "providers"),
])
async def test_malformed_json_is_reported_and_never_overwritten(service, filename, operation):
    service.config_dir.mkdir(parents=True)
    path = service.config_dir / filename
    damaged = b'{"keep-this-user-data": '
    path.write_bytes(damaged)
    with pytest.raises(SettingsError):
        if operation == "general":
            await service.update_general_settings(GeneralSettingsUpdate(max_retries=2))
        else:
            await service.update_provider("custom", ProviderUpdateRequest(
                base_url="https://custom.example/v1", default_models=["model-a"], api_key="new-secret",
            ))
    assert path.read_bytes() == damaged


@pytest.mark.asyncio
async def test_concurrent_partial_updates_preserve_both_fields(service):
    await init_db(service.db_path)
    await asyncio.gather(
        service.update_general_settings(GeneralSettingsUpdate(default_target_lang="en")),
        service.update_general_settings(GeneralSettingsUpdate(max_retries=8)),
    )
    result = await fresh(service).get_general_settings()
    assert result.default_target_lang == "en"
    assert result.max_retries == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("style,base,path,key_header", [
    ("openai", "https://proxy.example/prefix/v1", "/prefix/v1/models", "authorization"),
    ("anthropic", "https://proxy.example/prefix", "/prefix/v1/models", "x-api-key"),
    ("gemini", "https://proxy.example/prefix", "/prefix/v1beta/models", "x-goog-api-key"),
])
@pytest.mark.parametrize("model", [None, "model-a"])
async def test_connectivity_protocol_and_explicit_model(service, mock_http, style, base, path, key_header, model):
    write_providers(service, {"custom": {
        "api_style": style, "base_url": base, "default_models": ["unused-default"],
    }})
    requests = mock_http()
    response = await service.test_provider("custom", ProviderTestRequest(
        api_key="sk-private-secret", model=model,
    ))
    assert response.success is True
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert str(request.url).startswith(base)
    assert request.url.path == path + (f"/{model}" if model else "")
    assert "sk-private-secret" not in str(request.url)
    assert not request.url.query
    expected = "Bearer sk-private-secret" if style == "openai" else "sk-private-secret"
    assert request.headers[key_header] == expected
    if style == "anthropic":
        assert request.headers["anthropic-version"]
    assert request.extensions["timeout"]["connect"] > 0
    assert request.extensions["timeout"]["read"] > 0
    assert not request.content


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 403, 404, 429, 500])
async def test_connectivity_status_failure_no_redirect_fallback_or_body_leak(service, mock_http, status, caplog):
    requests = mock_http(status=status, body={"error": "sensitive-upstream-error sk-private-secret"},
                         headers={"location": "https://other.example/steal"})
    response = await service.test_provider("openai", ProviderTestRequest(api_key="sk-private-secret"))
    assert response.success is False
    assert len(requests) == 1
    assert "sk-private-secret" not in response.message + caplog.text
    assert "sensitive-upstream-error" not in response.message + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout])
async def test_connectivity_transport_errors_are_sanitized(service, mock_http, error, caplog):
    requests = mock_http(error=error)
    response = await service.test_provider("openai", ProviderTestRequest(api_key="sk-private-secret"))
    assert response.success is False
    assert len(requests) == 1
    assert "sk-private-secret" not in response.message + caplog.text
    assert "sensitive-upstream-error" not in response.message + caplog.text


def test_router_persistence_and_mocked_ping(client, mock_http):
    assert client.get("/api/settings/general").json()["default_target_lang"] == "zh-hans"
    response = client.put("/api/settings/general", json={"bilingual_style": "reversed"})
    assert response.status_code == 200
    assert response.json()["bilingual_style"] == "reversed"
    response = client.put("/api/settings/providers/custom", json={
        "base_url": "https://custom.example/v1", "api_key": "sk-router-secret-12345678",
        "default_models": ["model-a"], "set_as_active": True,
    })
    assert response.status_code == 200
    assert response.json()["has_api_key"] is True
    assert "sk-router-secret-12345678" not in response.text
    assert client.get("/api/settings/providers").json()["active_provider"] == "custom"
    requests = mock_http()
    response = client.post("/api/settings/providers/custom/test", json={"model": "model-a"})
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer sk-router-secret-12345678"
    assert requests[0].url.path == "/v1/models/model-a"


@pytest.mark.parametrize("path,payload", [
    ("general", {"bilingual_style": "sideways"}),
    ("general", {"concurrency_limit": 0}),
    ("general", {"max_retries": -1}),
    ("providers/custom", {"default_models": []}),
    ("providers/custom", {"base_url": "file:///etc/passwd"}),
])
def test_router_translates_validation_errors(client, path, payload):
    response = client.put(f"/api/settings/{path}", json=payload)
    assert response.status_code in (400, 422)
    assert "detail" in response.json()


@pytest.mark.asyncio
async def test_atomic_write_failure_leaves_original_json(service, monkeypatch):
    await service.update_general_settings(GeneralSettingsUpdate(max_retries=4))
    path = service.config_dir / "general.json"
    before = path.read_bytes()

    def fail_replace(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(settings_module.os, "replace", fail_replace)
    with pytest.raises(SettingsError) as error:
        await service.update_general_settings(GeneralSettingsUpdate(max_retries=7))
    assert error.value.status_code == 500
    assert path.read_bytes() == before
    assert (await fresh(service).get_general_settings()).max_retries == 4


@pytest.mark.asyncio
async def test_concurrent_provider_updates_keep_both_keys_and_definitions(service):
    await asyncio.gather(*(
        service.update_provider(name, ProviderUpdateRequest(
            base_url=f"https://{name}.example/v1", default_models=["model-a"], api_key=f"secret-{name}",
        )) for name in ("one", "two")
    ))
    restarted = fresh(service)
    keys = await restarted.get_saved_keys()
    listing = await restarted.get_providers()
    for name in ("one", "two"):
        assert keys[name] == f"secret-{name}"
        assert listing.providers[name].base_url == f"https://{name}.example/v1"


@pytest.mark.asyncio
@pytest.mark.parametrize("style,base,model,expected_path", [
    ("anthropic", "https://proxy.example/prefix/v1", None, "/prefix/v1/models"),
    ("gemini", "https://proxy.example/prefix/v1beta", "models/gemini-test", "/prefix/v1beta/models/gemini-test"),
    ("gemini", "https://proxy.example/prefix/v1", None, "/prefix/v1/models"),
])
async def test_connectivity_does_not_duplicate_version_or_models_prefix(service, mock_http, style, base, model, expected_path):
    write_providers(service, {"custom": {"api_style": style, "base_url": base}})
    requests = mock_http()
    response = await service.test_provider("custom", ProviderTestRequest(model=model, api_key="secret"))
    assert response.success
    assert len(requests) == 1
    assert requests[0].url.path == expected_path


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"base_url": "ftp://proxy.example"},
    {"base_url": "https://proxy.example/v1?key=secret"},
    {"base_url": "https://proxy.example/v1#fragment"},
    {"model": "   "},
])
async def test_invalid_connectivity_inputs_fail_before_http(service, payload):
    with pytest.raises(SettingsError) as error:
        await service.test_provider("openai", ProviderTestRequest(**payload))
    assert error.value.status_code in (400, 422)


def test_router_translates_missing_provider(client):
    response = client.post("/api/settings/providers/nonexistent/test", json={})
    assert response.status_code == 404
    assert "detail" in response.json()


def test_router_translates_corrupt_configuration_without_overwrite(client, service):
    path = service.config_dir / "general.json"
    path.write_text("{broken", encoding="utf-8")
    response = client.put("/api/settings/general", json={"max_retries": 2})
    assert response.status_code == 500
    assert "detail" in response.json()
    assert path.read_text(encoding="utf-8") == "{broken"
