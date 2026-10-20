"""Settings router stubs for global preferences and provider configurations.

This module will be fully implemented by Issue #4 (Settings & Provider Manager).
"""

from fastapi import APIRouter
from server.schemas import (
    GeneralSettings,
    GeneralSettingsUpdate,
    ProvidersListResponse,
    ProviderInfo,
    ProviderUpdateRequest,
    ProviderTestRequest,
    ProviderTestResponse,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Initial in-memory mock settings for stub responses
_MOCK_GENERAL_SETTINGS = GeneralSettings(
    default_target_lang="zh-hans",
    bilingual_style="default",
    concurrency_limit=1,
    max_retries=3,
)

_MOCK_PROVIDERS: dict[str, ProviderInfo] = {
    "openai": ProviderInfo(
        api_style="openai",
        base_url="https://api.openai.com/v1",
        default_models=["gpt-4o", "gpt-4o-mini"],
        has_api_key=True,
        masked_api_key="sk-proj-****AbCd",
    ),
    "gemini": ProviderInfo(
        api_style="gemini",
        base_url=None,
        default_models=["gemini-1.5-pro", "gemini-1.5-flash"],
        has_api_key=False,
        masked_api_key=None,
    ),
}


@router.get(
    "/general",
    response_model=GeneralSettings,
    summary="Get global system translation and runtime settings",
)
async def get_general_settings() -> GeneralSettings:
    """Return current global preferences."""
    return _MOCK_GENERAL_SETTINGS


@router.put(
    "/general",
    response_model=GeneralSettings,
    summary="Update global system translation and runtime settings",
)
async def update_general_settings(payload: GeneralSettingsUpdate) -> GeneralSettings:
    """Update global preferences with provided fields."""
    global _MOCK_GENERAL_SETTINGS
    data = _MOCK_GENERAL_SETTINGS.model_dump()
    updates = payload.model_dump(exclude_unset=True)
    data.update(updates)
    _MOCK_GENERAL_SETTINGS = GeneralSettings(**data)
    return _MOCK_GENERAL_SETTINGS


@router.get(
    "/providers",
    response_model=ProvidersListResponse,
    summary="List all configured translation providers with masked API keys",
)
async def get_providers() -> ProvidersListResponse:
    """Return provider configurations with sensitive keys masked."""
    return ProvidersListResponse(
        active_provider="openai",
        providers=_MOCK_PROVIDERS,
    )


@router.put(
    "/providers/{provider_name}",
    response_model=ProviderInfo,
    summary="Update or create a provider configuration",
)
async def update_provider(
    provider_name: str,
    payload: ProviderUpdateRequest,
) -> ProviderInfo:
    """Update or insert a provider definition."""
    existing = _MOCK_PROVIDERS.get(
        provider_name,
        ProviderInfo(
            api_style="openai",
            base_url=payload.base_url,
            default_models=payload.default_models or ["default-model"],
            has_api_key=bool(payload.api_key),
            masked_api_key="sk-****" if payload.api_key else None,
        ),
    )
    if payload.base_url is not None:
        existing.base_url = payload.base_url
    if payload.default_models is not None:
        existing.default_models = payload.default_models
    if payload.api_key:
        existing.has_api_key = True
        existing.masked_api_key = f"{payload.api_key[:4]}****{payload.api_key[-4:]}" if len(payload.api_key) > 8 else "****"

    _MOCK_PROVIDERS[provider_name] = existing
    return existing


@router.post(
    "/providers/{provider_name}/test",
    response_model=ProviderTestResponse,
    summary="Test provider connectivity and API key validity",
)
async def test_provider(
    provider_name: str,
    payload: ProviderTestRequest,
) -> ProviderTestResponse:
    """Simulate provider ping connection test."""
    return ProviderTestResponse(
        success=True,
        message=f"Provider '{provider_name}' connected successfully. Simulated latency 120ms.",
    )
