"""Pydantic schemas and API contracts for bilingual_book_maker web service.

Strictly aligned with docs/local/parallel_development_spec.md (Section 3).
"""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

# ============================================================================
# 1. Common Schemas
# ============================================================================


class ErrorResponse(BaseModel):
    """Standard error response structure."""

    detail: str = Field(..., description="Error description message")
    code: Optional[str] = Field(None, description="Specific error code identifier")


# ============================================================================
# 2. Authentication Schemas (Section 3.2)
# ============================================================================


class AuthStatusResponse(BaseModel):
    """Authentication status check response."""

    protected_mode: bool = Field(
        ...,
        description="true: AUTH_PASSWORD configured (protected); false: open access",
    )
    authenticated: bool = Field(
        ...,
        description="Whether current request carries valid session or token",
    )


class LoginRequest(BaseModel):
    """Password login payload."""

    password: str = Field(..., description="Access password")


class LoginResponse(BaseModel):
    """Successful login response."""

    success: bool = True
    detail: str = "Login successful"


# ============================================================================
# 3. Tasks Management Schemas (Section 3.3)
# ============================================================================


class TaskCreateResponse(BaseModel):
    """Brief representation of a newly created/queued task."""

    id: str = Field(..., description="Task unique identifier")
    filename: str = Field(..., description="Original book filename")
    status: str = Field("pending", description="Initial task status")
    created_at: Optional[str] = Field(None, description="Task creation ISO timestamp")


class TaskResponse(BaseModel):
    """Detailed task object."""

    id: str = Field(..., description="Task unique identifier")
    filename: str = Field(..., description="Original book filename")
    file_type: Optional[str] = Field(
        None, description="Book file format (e.g. epub, pdf, txt, srt)"
    )
    status: str = Field(
        ..., description="Task status (pending, running, completed, failed, cancelled)"
    )
    progress_current: int = Field(
        0, description="Current translated chunks/paragraphs count"
    )
    progress_total: int = Field(0, description="Total chunks/paragraphs to translate")
    model_provider: Optional[str] = Field(
        None, description="Translation service provider (e.g. openai)"
    )
    target_language: str = Field(
        "zh-hans", description="Target translation language code"
    )
    prompt_template: Optional[str] = Field(
        None, description="Custom prompt template used"
    )
    error_message: Optional[str] = Field(
        None, description="Error message summary if failed"
    )
    created_at: Optional[str] = Field(None, description="Creation timestamp")
    updated_at: Optional[str] = Field(None, description="Last update timestamp")


class TaskListResponse(BaseModel):
    """Paginated task list response."""

    total: int = Field(..., description="Total tasks count matching query")
    items: List[TaskResponse] = Field(
        default_factory=list, description="List of task objects"
    )


class TaskCancelResponse(BaseModel):
    """Response returned when a task is cancelled."""

    id: str = Field(..., description="Task unique identifier")
    status: str = Field("cancelled", description="Task status after cancellation")


class LogMessage(BaseModel):
    """Structured log frame delivered via Server-Sent Events (SSE)."""

    time: str = Field(..., description="Timestamp of log event, e.g. HH:MM:SS")
    line: str = Field(..., description="Log content text")


# ============================================================================
# 4. Settings Center Schemas (Section 3.4)
# ============================================================================


class GeneralSettings(BaseModel):
    """Global translation & system runtime preferences."""

    default_target_lang: str = Field(
        "zh-hans", description="Default target translation language"
    )
    bilingual_style: str = Field(
        "default",
        description="Layout style: 'default' (target below source), 'reversed' (target above source), 'only' (target only)",
    )
    concurrency_limit: int = Field(
        1, description="Max concurrent translation tasks running simultaneously"
    )
    max_retries: int = Field(3, description="Max retry attempts for failed API calls")


class GeneralSettingsUpdate(BaseModel):
    """Payload for updating global settings (partial updates supported)."""

    default_target_lang: Optional[str] = None
    bilingual_style: Optional[str] = None
    concurrency_limit: Optional[int] = None
    max_retries: Optional[int] = None


class ProviderInfo(BaseModel):
    """Provider configuration representation with sensitive API Key masked."""

    api_style: str = Field(
        ..., description="API communication style (e.g. openai, gemini, anthropic)"
    )
    base_url: Optional[str] = Field(None, description="Custom API endpoint base URL")
    default_models: List[str] = Field(
        default_factory=list, description="Available / default models list"
    )
    has_api_key: bool = Field(
        False, description="Whether an API key is configured for this provider"
    )
    masked_api_key: Optional[str] = Field(
        None, description="Masked API key preview, e.g. sk-proj-****AbCd"
    )


class ProvidersListResponse(BaseModel):
    """Response containing all available providers and currently active provider."""

    active_provider: Optional[str] = Field(
        None, description="Currently selected default provider name"
    )
    providers: Dict[str, ProviderInfo] = Field(
        default_factory=dict,
        description="Map of provider name to its metadata and masked config",
    )


class ProviderUpdateRequest(BaseModel):
    """Payload for updating or creating a provider configuration."""

    base_url: Optional[str] = Field(None, description="Custom API endpoint base URL")
    api_key: Optional[str] = Field(
        None,
        description="Real API key string (leave None or empty if unchanged)",
    )
    default_models: Optional[List[str]] = Field(
        None, description="Updated list of models"
    )
    set_as_active: Optional[bool] = Field(
        False, description="Set this provider as active default"
    )


class ProviderTestRequest(BaseModel):
    """Request payload for testing provider connectivity (ping test)."""

    base_url: Optional[str] = Field(
        None, description="Optional custom base URL to test"
    )
    api_key: Optional[str] = Field(
        None, description="Optional API key to test (uses saved key if empty)"
    )
    model: Optional[str] = Field(None, description="Optional specific model to test")


class ProviderTestResponse(BaseModel):
    """Response returned from provider connectivity test."""

    success: bool = Field(
        ..., description="True if connection and test translation succeeded"
    )
    message: str = Field(..., description="Latency info or error description")
