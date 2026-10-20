"""Dual-mode authentication and security middleware.

Implements zero-external-dependency HMAC-SHA256 session token management,
bearer token validation, path whitelisting, and pure ASGI middleware
for open vs protected mode execution.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Optional, Tuple, Set

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from server import config

logger = logging.getLogger("server.middleware.auth")

# Static paths that are always exempt from authentication
EXACT_WHITELIST_PATHS: Set[str] = {
    "/",
    "/favicon.ico",
    "/favicon.svg",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/health",
    "/api/auth/status",
    "/api/auth/login",
}

PREFIX_WHITELIST_PATHS: Tuple[str, ...] = (
    "/static/",
)


class SessionManager:
    """Manages creation and verification of HMAC-SHA256 signed session tokens."""

    DEFAULT_MAX_AGE: int = 7 * 86400  # 7 days in seconds

    @staticmethod
    def _derive_signing_key(password: str) -> bytes:
        """Derive an HMAC signing key from the current AUTH_PASSWORD."""
        salt = b"bbm-session-v1-salt:"
        return hashlib.sha256(salt + password.encode("utf-8")).digest()

    @classmethod
    def create_session_token(cls, password: str, max_age: Optional[int] = None) -> str:
        """Create a cryptographically signed session token.

        Format: <base64url_json_payload>.<hmac_sha256_hex>
        """
        if not password:
            raise ValueError("Cannot create session token without a valid password")

        validity = max_age if max_age is not None else cls.DEFAULT_MAX_AGE
        now = int(time.time())
        payload = {
            "created_at": now,
            "exp": now + validity,
        }
        payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")

        key = cls._derive_signing_key(password)
        signature = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).hexdigest()

        return f"{payload_b64}.{signature}"

    @classmethod
    def verify_session_token(cls, token: str, password: str) -> bool:
        """Verify the integrity, signature, and expiration of a session token."""
        if not token or not password:
            return False

        parts = token.split(".")
        if len(parts) != 2:
            return False

        payload_b64, signature = parts
        key = cls._derive_signing_key(password)
        expected_sig = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).hexdigest()

        # Constant-time comparison to prevent timing attacks
        if not hmac.compare_digest(signature, expected_sig):
            return False

        # Add back base64 padding if needed
        padding = 4 - (len(payload_b64) % 4)
        if padding != 4:
            payload_b64_padded = payload_b64 + ("=" * padding)
        else:
            payload_b64_padded = payload_b64

        try:
            payload_bytes = base64.urlsafe_b64decode(payload_b64_padded)
            payload = json.loads(payload_bytes.decode("utf-8"))
            exp = payload.get("exp")
            if exp is None or not isinstance(exp, (int, float)):
                return False
            if time.time() > exp:
                return False  # Expired
            return True
        except Exception:
            return False


def is_path_whitelisted(path: str) -> bool:
    """Check if the given HTTP path is exempt from authentication."""
    # Normalize trailing slashes for exact match (except root "/")
    norm_path = path.rstrip("/") if path != "/" else "/"

    if norm_path in EXACT_WHITELIST_PATHS or path in EXACT_WHITELIST_PATHS:
        return True

    for prefix in PREFIX_WHITELIST_PATHS:
        if path.startswith(prefix):
            return True

    return False


def extract_bearer_token(auth_header: Optional[str]) -> Optional[str]:
    """Extract token string from 'Bearer <token>' Authorization header."""
    if not auth_header:
        return None
    parts = auth_header.strip().split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def extract_cookie_from_headers(headers: Headers, cookie_name: str) -> Optional[str]:
    """Extract a specific cookie value from HTTP headers."""
    cookie_str = headers.get("cookie")
    if not cookie_str:
        return None

    # Parse simple cookie string: a=b; c=d
    for item in cookie_str.split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            key, val = item.split("=", 1)
            if key.strip() == cookie_name:
                return val.strip()
    return None


def is_authenticated_request(headers: Headers, password: str) -> bool:
    """Verify if request headers contain valid Bearer token or valid session cookie."""
    if not password:
        return True

    # 1. Check Bearer token in Authorization header
    auth_header = headers.get("authorization")
    token = extract_bearer_token(auth_header)
    if token and secrets.compare_digest(token, password):
        return True

    # 2. Check bbm_session Cookie
    session_cookie = extract_cookie_from_headers(headers, config.SESSION_COOKIE_NAME)
    if session_cookie and SessionManager.verify_session_token(session_cookie, password):
        return True

    return False


class DualModeAuthMiddleware:
    """Pure ASGI middleware enforcing dual-mode authentication.

    - Open Mode (AUTH_PASSWORD unset/empty): passes all requests directly.
    - Protected Mode (AUTH_PASSWORD set): permits whitelisted paths, OPTIONS requests,
      and requests authenticated via Bearer token or session cookie.
      Rejects unauthenticated requests with HTTP 401 JSON.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Check if protected mode is active dynamically
        if not config.is_protected_mode():
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        # Always allow CORS preflight OPTIONS requests through
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # Check whitelist paths
        if is_path_whitelisted(path):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        current_password = config.AUTH_PASSWORD

        if is_authenticated_request(headers, current_password):
            await self.app(scope, receive, send)
            return

        # Unauthorized: send 401 JSON response
        error_body = json.dumps(
            {
                "detail": "未授权访问，请先登录",
                "code": "UNAUTHORIZED",
            },
            ensure_ascii=False,
        ).encode("utf-8")

        response_headers = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(error_body)).encode("ascii")),
        ]

        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": response_headers,
        })
        await send({
            "type": "http.response.body",
            "body": error_body,
            "more_body": False,
        })
