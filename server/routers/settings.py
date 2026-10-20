"""Settings router for global preferences and provider configurations."""

from fastapi import APIRouter, HTTPException
from server.schemas import (
    GeneralSettings,
    GeneralSettingsUpdate,
    ProvidersListResponse,
    ProviderInfo,
    ProviderUpdateRequest,
    ProviderTestRequest,
    ProviderTestResponse,
)
from server.services.settings import SettingsError, settings_service

router = APIRouter(prefix="/api/settings", tags=["settings"])


async def _call(operation, *args):
    try:
        return await operation(*args)
    except SettingsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None


@router.get(
    "/general",
    response_model=GeneralSettings,
    summary="Get global system translation and runtime settings",
)
async def get_general_settings() -> GeneralSettings:
    """Return current global preferences."""
    return await _call(settings_service.get_general_settings)


@router.put(
    "/general",
    response_model=GeneralSettings,
    summary="Update global system translation and runtime settings",
)
async def update_general_settings(payload: GeneralSettingsUpdate) -> GeneralSettings:
    """Update global preferences with provided fields."""
    return await _call(settings_service.update_general_settings, payload)


@router.get(
    "/providers",
    response_model=ProvidersListResponse,
    summary="List all configured translation providers with masked API keys",
)
async def get_providers() -> ProvidersListResponse:
    """Return provider configurations with sensitive keys masked."""
    return await _call(settings_service.get_providers)


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
    return await _call(settings_service.update_provider, provider_name, payload)


@router.post(
    "/providers/{provider_name}/test",
    response_model=ProviderTestResponse,
    summary="Test provider connectivity and API key validity",
)
async def test_provider(
    provider_name: str,
    payload: ProviderTestRequest,
) -> ProviderTestResponse:
    """Perform lightweight connectivity test against the provider."""
    return await _call(settings_service.test_provider, provider_name, payload)
