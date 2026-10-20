"""Authentication router stubs.

This module will be fully implemented by Issue #3 (Dual-mode Auth & Security).
"""

from fastapi import APIRouter, Response, HTTPException, status
from server import config
from server.schemas import (
    AuthStatusResponse,
    LoginRequest,
    LoginResponse,
    ErrorResponse,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get(
    "/status",
    response_model=AuthStatusResponse,
    summary="Check authentication mode and current session status",
)
async def get_auth_status() -> AuthStatusResponse:
    """Returns whether the server is in protected mode and if the requester is authenticated."""
    is_protected = config.is_protected_mode()
    # In open mode, every request is automatically authenticated.
    return AuthStatusResponse(
        protected_mode=is_protected,
        authenticated=not is_protected,
    )


@router.post(
    "/login",
    response_model=LoginResponse,
    responses={401: {"model": ErrorResponse}},
    summary="Login with password",
)
async def login(
    payload: LoginRequest,
    response: Response,
) -> LoginResponse:
    """Validate password and set session cookie. (Stub implementation)"""
    if config.is_protected_mode():
        if payload.password != config.AUTH_PASSWORD:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="密码错误",
            )
        # Mock session cookie
        response.set_cookie(
            key=config.SESSION_COOKIE_NAME,
            value="mock_valid_session_token",
            httponly=True,
            samesite="lax",
            path="/",
        )
    return LoginResponse(success=True, detail="Login successful")


@router.post(
    "/logout",
    response_model=LoginResponse,
    summary="Logout current session",
)
async def logout(response: Response) -> LoginResponse:
    """Clear session cookie."""
    response.delete_cookie(key=config.SESSION_COOKIE_NAME, path="/")
    return LoginResponse(success=True, detail="Logout successful")
