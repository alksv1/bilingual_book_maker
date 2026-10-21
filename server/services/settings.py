"""Persistent web settings and credential-safe provider connectivity probes.

Provider entries contain only fields understood by book_maker. Secrets live in
keys.json (plaintext, owner-readable on POSIX), never in API responses. Runners
must merge get_provider_environment() into their subprocess environment and load
bbm_providers.json from this service's config directory. No global environment
variables are overwritten: two providers may otherwise share a vendor key name.
"""

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from book_maker.provider_loader import validate_provider
from server import config
from server.schemas import (
    GeneralSettings,
    GeneralSettingsUpdate,
    ProviderInfo,
    ProvidersListResponse,
    ProviderUpdateRequest,
    ProviderTestRequest,
    ProviderTestResponse,
)

# Serialize read/modify/write across service instances in this server process.
_SETTINGS_LOCK = threading.RLock()
_DEFAULT_BASES = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "claude": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "groq": "https://api.groq.com/openai/v1",
    "xai": "https://api.x.ai/v1",
}


class SettingsError(Exception):
    """Safe error text that may be returned to the API caller."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def mask_api_key(key: str | None) -> str | None:
    key = (key or "").strip()
    if not key:
        return None
    return "****" if len(key) <= 8 else f"{key[:4]}****{key[-4:]}"


def _read_object(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError):
        raise SettingsError(
            500, "Configuration file is invalid; repair it before saving."
        ) from None
    if not isinstance(data, dict):
        raise SettingsError(500, "Configuration must be a JSON object.")
    return data


def _write_object(path: Path, data: dict) -> None:
    """Replace only after a complete write; failed writes leave the old file intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _validate_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        parsed.port  # Validate the port as well as the hostname.
        valid = (
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and not any(c.isspace() or ord(c) < 32 for c in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise SettingsError(
            422, "Base URL must be HTTP(S), without credentials, query or fragment."
        )
    return value.rstrip("/")


class SettingsService:
    def __init__(self, config_dir: Path | None = None, db_path: Path | None = None):
        self._config_dir = config_dir
        self._db_path = db_path

    @property
    def config_dir(self) -> Path:
        return self._config_dir or config.CONFIG_DIR

    @property
    def db_path(self) -> Path:
        return self._db_path or config.DB_PATH

    @property
    def general_config_path(self) -> Path:
        return self.config_dir / "general.json"

    @property
    def providers_config_path(self) -> Path:
        return self.config_dir / "bbm_providers.json"

    @property
    def keys_config_path(self) -> Path:
        return self.config_dir / "keys.json"

    async def _run(self, operation, *args):
        def locked():
            with _SETTINGS_LOCK:
                try:
                    return operation(*args)
                except (OSError, sqlite3.Error):
                    raise SettingsError(
                        500, "Unable to read or persist settings."
                    ) from None

        return await asyncio.to_thread(locked)

    def _db_get(self, key):
        if not self.db_path.exists():
            return None
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def _db_set(self, key, value):
        # JSON remains authoritative; the application lifespan initializes SQLite.
        if self.db_path.exists():
            with sqlite3.connect(self.db_path) as db:
                db.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                    (key, value),
                )

    def _general(self):
        data = _read_object(self.general_config_path)
        if data is None:
            raw = self._db_get("general_settings")
            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                raise SettingsError(
                    500, "Stored general settings are invalid."
                ) from None
        if not isinstance(data, dict):
            raise SettingsError(500, "Stored general settings must be an object.")
        return data

    @staticmethod
    def _general_model(data):
        values = GeneralSettings().model_dump()
        values.update({k: v for k, v in data.items() if k in values})
        if (
            not isinstance(values["default_target_lang"], str)
            or not values["default_target_lang"].strip()
        ):
            raise SettingsError(422, "Default target language must not be blank.")
        if values["bilingual_style"] not in ("default", "reversed", "only"):
            raise SettingsError(422, "Invalid bilingual style.")
        for field, minimum in (("concurrency_limit", 1), ("max_retries", 0)):
            if type(values[field]) is not int or values[field] < minimum:
                raise SettingsError(422, f"{field} must be an integer >= {minimum}.")
        return GeneralSettings(**values)

    async def get_general_settings(self) -> GeneralSettings:
        return await self._run(lambda: self._general_model(self._general()))

    async def update_general_settings(
        self, payload: GeneralSettingsUpdate
    ) -> GeneralSettings:
        def update():
            data = self._general()
            data.update(payload.model_dump(exclude_unset=True))
            result = self._general_model(data)
            data.update(result.model_dump())
            _write_object(self.general_config_path, data)
            self._db_set("general_settings", json.dumps(result.model_dump()))
            return result

        return await self._run(update)

    def _keys(self):
        keys = _read_object(self.keys_config_path) or {}
        if not all(isinstance(v, str) for v in keys.values()):
            raise SettingsError(500, "Stored provider keys are invalid.")
        return keys

    async def get_saved_keys(self) -> dict[str, str]:
        """Internal credential access; never expose through a route."""
        return await self._run(self._keys)

    def _providers_document(self):
        document = _read_object(self.providers_config_path)
        if document is None:
            example = _read_object(config.BASE_DIR / "bbm_providers.example.json") or {
                "providers": {}
            }
            document = {
                "providers": {
                    name: item
                    for name, item in example.get("providers", {}).items()
                    if "FILL-ME" not in json.dumps(item)
                }
            }
        providers = document.get("providers")
        if not isinstance(providers, dict):
            raise SettingsError(
                500, "Provider configuration must contain a providers object."
            )
        for name, entry in providers.items():
            try:
                validate_provider(name, entry)
            except (ValueError, TypeError):
                raise SettingsError(
                    500, "Stored provider definition is invalid."
                ) from None
        return document

    def _active(self, providers):
        general = self._general()
        active = general.get("active_provider", self._db_get("active_provider"))
        if active in providers:
            return active
        return "openai" if "openai" in providers else next(iter(providers), None)

    async def get_active_provider(self) -> str | None:
        return await self._run(
            lambda: self._active(self._providers_document()["providers"])
        )

    @staticmethod
    def _key(name, entry, keys):
        return (
            os.environ.get(entry.get("env_key") or "", "").strip()
            or keys.get(name, "").strip()
        )

    def _info(self, name, entry, keys):
        key = self._key(name, entry, keys)
        return ProviderInfo(
            api_style=entry["api_style"],
            base_url=entry.get("base_url"),
            default_models=entry.get("default_models") or [],
            has_api_key=bool(key),
            masked_api_key=mask_api_key(key),
        )

    async def get_providers(self) -> ProvidersListResponse:
        def read():
            providers = self._providers_document()["providers"]
            keys = self._keys()
            return ProvidersListResponse(
                active_provider=self._active(providers),
                providers={
                    name: self._info(name, entry, keys)
                    for name, entry in providers.items()
                },
            )

        return await self._run(read)

    async def get_provider_environment(self) -> dict[str, str]:
        """Nonblank saved credentials; runners must preserve nonblank ambient values."""

        def read():
            keys = self._keys()
            return {
                entry["env_key"]: keys[name].strip()
                for name, entry in self._providers_document()["providers"].items()
                if keys.get(name, "").strip() and entry.get("env_key")
            }

        return await self._run(read)

    async def update_provider(
        self, provider_name: str, payload: ProviderUpdateRequest
    ) -> ProviderInfo:
        def update():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", provider_name):
                raise SettingsError(422, "Invalid provider name.")
            document = self._providers_document()
            providers = document["providers"]
            entry = dict(providers.get(provider_name, {"api_style": "openai"}))
            keys = self._keys()
            general = self._general()  # Refuse to overwrite malformed files.
            if payload.base_url is not None:
                entry["base_url"] = _validate_url(payload.base_url.strip())
            if payload.default_models is not None:
                entry["default_models"] = payload.default_models
            try:
                validate_provider(provider_name, entry)
            except (ValueError, TypeError):
                raise SettingsError(
                    422, "Invalid provider definition or model list."
                ) from None
            new_key = (payload.api_key or "").strip()
            if new_key:
                if any(ord(c) < 32 or ord(c) > 126 for c in new_key):
                    raise SettingsError(
                        422, "API key must contain printable ASCII characters."
                    )
                # A shared OPENAI_API_KEY must not change another provider's credentials.
                entry["env_key"] = (
                    "BBM_WEB_"
                    + hashlib.sha256(provider_name.encode()).hexdigest().upper()
                    + "_API_KEY"
                )
                keys[provider_name] = new_key
                _write_object(self.keys_config_path, keys)
            providers[provider_name] = entry
            _write_object(self.providers_config_path, document)
            if payload.set_as_active:
                general["active_provider"] = provider_name
                _write_object(self.general_config_path, general)
                self._db_set("active_provider", provider_name)
            return self._info(provider_name, entry, keys)

        return await self._run(update)

    async def test_provider(
        self, provider_name: str, payload: ProviderTestRequest
    ) -> ProviderTestResponse:
        def read():
            entry = self._providers_document()["providers"].get(provider_name)
            if entry is None:
                raise SettingsError(404, "Provider not found.")
            return dict(entry), self._key(provider_name, entry, self._keys())

        entry, saved_key = await self._run(read)
        style = entry["api_style"]
        if style not in (*_DEFAULT_BASES, "litellm"):
            return ProviderTestResponse(
                success=False,
                message="This provider does not support a models connectivity probe.",
            )
        base = _validate_url(
            (
                payload.base_url
                or entry.get("base_url")
                or _DEFAULT_BASES.get(style, "")
            ).strip()
        )
        key = (payload.api_key or "").strip() or saved_key
        if any(ord(c) < 32 or ord(c) > 126 for c in key):
            raise SettingsError(422, "Invalid API key characters.")
        headers = {}
        if style in ("anthropic", "claude"):
            base = base if base.endswith("/v1") else base + "/v1"
            headers["anthropic-version"] = "2023-06-01"
            if key:
                headers["x-api-key"] = key
        elif style == "gemini":
            base = base if base.endswith(("/v1", "/v1beta")) else base + "/v1beta"
            if key:
                headers["x-goog-api-key"] = key
        elif key:
            headers["Authorization"] = f"Bearer {key}"
        url = base + "/models"
        specific_url = None
        if payload.model:
            model = payload.model.strip()
            if not model:
                raise SettingsError(422, "Model must not be blank.")
            if style == "gemini" and model.startswith("models/"):
                model = model[len("models/") :]
            specific_url = url + "/" + quote(model, safe="")
        start = time.perf_counter()
        try:
            # Local endpoints are intentional (Ollama); never follow a redirect with a key.
            async with httpx.AsyncClient(
                timeout=10.0, follow_redirects=False, trust_env=False
            ) as client:
                target_url = specific_url or url
                response = await asyncio.wait_for(
                    client.get(target_url, headers=headers), timeout=10.0
                )
                # Many OpenAI-compatible relays/proxies only support GET /v1/models,
                # returning 404 for GET /v1/models/{model}. Fall back to /v1/models if so.
                if specific_url and response.status_code == 404:
                    fallback_response = await asyncio.wait_for(
                        client.get(url, headers=headers), timeout=10.0
                    )
                    if fallback_response.status_code == 200:
                        response = fallback_response
            latency = int((time.perf_counter() - start) * 1000)
            if response.status_code == 200:
                return ProviderTestResponse(
                    success=True,
                    message=f"连接成功，延迟 {latency}ms（模型接口，不执行翻译）",
                )
            if response.status_code in (401, 403):
                message = f"认证失败 ({response.status_code}): API Key 无效或未授权"
            else:
                message = f"模型接口请求失败 (HTTP {response.status_code})"
            return ProviderTestResponse(success=False, message=message)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            return ProviderTestResponse(success=False, message="连接超时 (10s)")
        except (httpx.HTTPError, httpx.InvalidURL, ValueError):
            # Remote bodies and exception text can echo keys or credential-bearing URLs.
            return ProviderTestResponse(
                success=False, message="连接失败，请检查端点和网络配置"
            )


settings_service = SettingsService()
