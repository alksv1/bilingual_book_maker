"""Authentication router providing status check, login, and logout endpoints.

Implements in-memory rate limiting / anti-brute force protection,
HMAC session generation, and cookie management.
"""

import logging
import secrets
import time
from collections import deque
from typing import Dict, Tuple, Optional

from fastapi import APIRouter, Request, Response, HTTPException, status
from starlette.datastructures import Headers

from server import config
from server.middleware.auth import SessionManager, is_authenticated_request
from server.schemas import (
    AuthStatusResponse,
    LoginRequest,
    LoginResponse,
    ErrorResponse,
)

logger = logging.getLogger("server.routers.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRateLimiter:
    """In-memory sliding window rate limiter to prevent brute force login attempts.

    Rules:
    - Tracks failed login attempts per client IP.
    - If `max_failures` (default: 5) are reached within `window_seconds` (default: 60s),
      the IP is blocked for `lockout_seconds` (default: 300s / 5min).
    - Success resets the counter for that IP.
    """

    def __init__(
        self,
        max_failures: int = 5,
        window_seconds: int = 60,
        lockout_seconds: int = 300,
    ):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds

        # Map IP -> deque of failure timestamps
        self._failures: Dict[str, deque] = {}
        # Map IP -> locked_until timestamp
        self._lockouts: Dict[str, float] = {}

    def _cleanup(self, now: float) -> None:
        """Evict expired lockouts and failure records to prevent unbounded memory growth."""
        # Evict expired lockouts
        expired_lockouts = [
            ip for ip, lock_time in self._lockouts.items() if now >= lock_time
        ]
        for ip in expired_lockouts:
            del self._lockouts[ip]

        # Evict old failure deques
        cutoff = now - self.window_seconds
        empty_ips = []
        for ip, timestamps in self._failures.items():
            while timestamps and timestamps[0] < cutoff:
                timestamps.popleft()
            if not timestamps and ip not in self._lockouts:
                empty_ips.append(ip)

        for ip in empty_ips:
            del self._failures[ip]

    def is_blocked(self, ip: str) -> Tuple[bool, int]:
        """Check if an IP is currently locked out.

        Returns (is_blocked: bool, remaining_seconds: int).
        """
        now = time.time()
        self._cleanup(now)

        if ip in self._lockouts:
            remaining = int(self._lockouts[ip] - now)
            if remaining > 0:
                return True, remaining
            else:
                del self._lockouts[ip]

        return False, 0

    def record_failure(self, ip: str) -> Tuple[bool, int]:
        """Record a failed login attempt for an IP.

        Returns (is_blocked: bool, remaining_seconds: int).
        """
        now = time.time()
        self._cleanup(now)

        if ip not in self._failures:
            self._failures[ip] = deque()

        self._failures[ip].append(now)

        # Count failures in current rolling window
        cutoff = now - self.window_seconds
        while self._failures[ip] and self._failures[ip][0] < cutoff:
            self._failures[ip].popleft()

        if len(self._failures[ip]) >= self.max_failures:
            # Trigger lockout
            locked_until = now + self.lockout_seconds
            self._lockouts[ip] = locked_until
            self._failures[ip].clear()
            logger.warning(
                "IP %s triggered login brute-force protection. Locked for %s seconds.",
                ip,
                self.lockout_seconds,
            )
            return True, self.lockout_seconds

        return False, 0

    def reset(self, ip: str) -> None:
        """Reset failed attempts and lockout on successful login."""
        self._failures.pop(ip, None)
        self._lockouts.pop(ip, None)


# Global instance of rate limiter
rate_limiter = LoginRateLimiter(
    max_failures=5,
    window_seconds=60,
    lockout_seconds=300,
)


def get_client_ip(request: Request) -> str:
    """Extract client IP address, respecting X-Forwarded-For if available."""
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        # First IP in X-Forwarded-For is client origin
        client_ip = forwarded_for.split(",")[0].strip()
        if client_ip:
            return client_ip
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


@router.get(
    "/status",
    response_model=AuthStatusResponse,
    summary="Check authentication mode and current session status",
)
async def get_auth_status(request: Request) -> AuthStatusResponse:
    """Returns whether the server is in protected mode and if the requester is authenticated."""
    is_protected = config.is_protected_mode()
    if not is_protected:
        # In open mode, all requests are considered authenticated
        return AuthStatusResponse(
            protected_mode=False,
            authenticated=True,
        )

    # Check request headers & session cookie
    authenticated = is_authenticated_request(request.headers, config.AUTH_PASSWORD)
    return AuthStatusResponse(
        protected_mode=True,
        authenticated=authenticated,
    )


@router.post(
    "/login",
    response_model=LoginResponse,
    responses={
        401: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
    },
    summary="Login with password",
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
) -> LoginResponse:
    """Validate password, enforce anti-brute-force rate limiting, and set session cookie."""
    client_ip = get_client_ip(request)

    # 1. Check rate limiter lockout
    blocked, remaining_seconds = rate_limiter.is_blocked(client_ip)
    if blocked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"登录尝试过于频繁，已被临时锁定，请在 {remaining_seconds} 秒后再试",
            headers={"Retry-After": str(remaining_seconds)},
        )

    # 2. Open mode: password not required
    if not config.is_protected_mode():
        return LoginResponse(success=True, detail="当前处于开放模式，无需密码验证")

    # 3. Verify password via constant-time comparison
    if not secrets.compare_digest(payload.password, config.AUTH_PASSWORD):
        is_now_blocked, lock_duration = rate_limiter.record_failure(client_ip)
        if is_now_blocked:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"密码错误次数过多，已被锁定 {lock_duration} 秒",
                headers={"Retry-After": str(lock_duration)},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="密码错误",
        )

    # 4. Successful login: reset rate limiter records
    rate_limiter.reset(client_ip)

    # 5. Generate signed session token and set HttpOnly Cookie
    session_token = SessionManager.create_session_token(
        password=config.AUTH_PASSWORD,
        max_age=SessionManager.DEFAULT_MAX_AGE,
    )

    response.set_cookie(
        key=config.SESSION_COOKIE_NAME,
        value=session_token,
        max_age=SessionManager.DEFAULT_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )

    return LoginResponse(success=True, detail="登录成功")


@router.post(
    "/logout",
    response_model=LoginResponse,
    summary="Logout current session",
)
async def logout(response: Response) -> LoginResponse:
    """Clear session cookie from client browser."""
    response.delete_cookie(key=config.SESSION_COOKIE_NAME, path="/")
    return LoginResponse(success=True, detail="登出成功")
