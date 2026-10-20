"""Unit and integration tests for dual-mode auth and security middleware (Issue #4)."""

import time
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from server.main import app
from server import config
from server.middleware.auth import (
    SessionManager,
    is_path_whitelisted,
    extract_bearer_token,
    extract_cookie_from_headers,
)
from server.routers.auth import LoginRateLimiter
from server.schemas import AuthStatusResponse, LoginResponse


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """Create a TestClient with temporary storage and clean config."""
    temp_data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", temp_data)
    monkeypatch.setattr(config, "CONFIG_DIR", temp_data / "config")
    monkeypatch.setattr(config, "UPLOADS_DIR", temp_data / "uploads")
    monkeypatch.setattr(config, "OUTPUT_DIR", temp_data / "output")
    monkeypatch.setattr(config, "LOGS_DIR", temp_data / "logs")
    monkeypatch.setattr(config, "DB_PATH", temp_data / "bbm_tasks.db")

    with TestClient(app) as test_client:
        yield test_client


# ============================================================================
# 1. SessionManager & Utilities Unit Tests
# ============================================================================


def test_session_token_creation_and_verification():
    """Test HMAC session token generation and verification."""
    password = "secret_password_123"
    token = SessionManager.create_session_token(password=password, max_age=3600)
    assert token is not None
    assert "." in token

    # Token must verify with the same password
    assert SessionManager.verify_session_token(token, password) is True

    # Token must fail with wrong password
    assert SessionManager.verify_session_token(token, "wrong_password") is False

    # Tampered signature must fail
    parts = token.split(".")
    tampered_sig = parts[0] + "." + "0" * len(parts[1])
    assert SessionManager.verify_session_token(tampered_sig, password) is False

    # Tampered payload must fail
    tampered_payload = "dGFtcGVyZWQ=" + "." + parts[1]
    assert SessionManager.verify_session_token(tampered_payload, password) is False

    # Expired token must fail
    expired_token = SessionManager.create_session_token(password=password, max_age=-1)
    assert SessionManager.verify_session_token(expired_token, password) is False


def test_session_token_empty_input():
    """Test handling of empty or None inputs to SessionManager."""
    with pytest.raises(ValueError):
        SessionManager.create_session_token("")

    assert SessionManager.verify_session_token("", "pw") is False
    assert SessionManager.verify_session_token("invalid.token", "") is False
    assert SessionManager.verify_session_token("invalid_no_dot", "pw") is False


def test_path_whitelist_matching():
    """Verify that whitelisted paths match accurately."""
    assert is_path_whitelisted("/") is True
    assert is_path_whitelisted("/favicon.ico") is True
    assert is_path_whitelisted("/favicon.svg") is True
    assert is_path_whitelisted("/docs") is True
    assert is_path_whitelisted("/docs/") is True
    assert is_path_whitelisted("/redoc") is True
    assert is_path_whitelisted("/openapi.json") is True
    assert is_path_whitelisted("/api/health") is True
    assert is_path_whitelisted("/api/auth/status") is True
    assert is_path_whitelisted("/api/auth/login") is True
    assert is_path_whitelisted("/static/css/style.css") is True
    assert is_path_whitelisted("/static/js/app.js") is True

    # Non-whitelisted paths must return False
    assert is_path_whitelisted("/api/tasks") is False
    assert is_path_whitelisted("/api/settings/general") is False
    assert is_path_whitelisted("/api/auth/logout") is False
    assert is_path_whitelisted("/api/random") is False


def test_bearer_token_extraction():
    """Test extraction of Bearer token from Authorization header."""
    assert extract_bearer_token(None) is None
    assert extract_bearer_token("") is None
    assert extract_bearer_token("Basic dXNlcjpwYXNz") is None
    assert extract_bearer_token("Bearer my_secret_token") == "my_secret_token"
    assert extract_bearer_token("bearer   my_secret_token  ") == "my_secret_token"


# ============================================================================
# 2. Open Mode (AUTH_PASSWORD unset / empty)
# ============================================================================


def test_open_mode_access(client: TestClient, monkeypatch):
    """In open mode (no password), all requests pass freely."""
    monkeypatch.setattr(config, "AUTH_PASSWORD", "")

    # Status check
    res = client.get("/api/auth/status")
    assert res.status_code == 200
    status_data = AuthStatusResponse(**res.json())
    assert status_data.protected_mode is False
    assert status_data.authenticated is True

    # Protected endpoints are accessible without any credentials
    res = client.get("/api/settings/general")
    assert res.status_code == 200

    res = client.get("/api/tasks")
    assert res.status_code == 200

    # Login in open mode returns success immediately
    res = client.post("/api/auth/login", json={"password": "any"})
    assert res.status_code == 200
    login_resp = LoginResponse(**res.json())
    assert login_resp.success is True

    # Health check is open
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["protected_mode"] is False


# ============================================================================
# 3. Protected Mode (AUTH_PASSWORD set)
# ============================================================================


def test_protected_mode_unauthorized(client: TestClient, monkeypatch):
    """In protected mode without credentials, protected endpoints return 401."""
    test_password = "secure_password_abc"
    monkeypatch.setattr(config, "AUTH_PASSWORD", test_password)

    # Status check indicates protected_mode=True, authenticated=False
    res = client.get("/api/auth/status")
    assert res.status_code == 200
    status_data = AuthStatusResponse(**res.json())
    assert status_data.protected_mode is True
    assert status_data.authenticated is False

    # Whitelisted endpoints remain accessible without auth
    assert client.get("/api/health").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200

    # Non-whitelisted endpoints return 401
    res = client.get("/api/settings/general")
    assert res.status_code == 401
    err = res.json()
    assert err["code"] == "UNAUTHORIZED"

    res = client.get("/api/tasks")
    assert res.status_code == 401
    assert res.json()["code"] == "UNAUTHORIZED"

    res = client.post("/api/auth/logout")
    assert res.status_code == 401


def test_protected_mode_bearer_token(client: TestClient, monkeypatch):
    """Bearer token matching AUTH_PASSWORD authorizes API requests."""
    test_password = "api_secret_token_789"
    monkeypatch.setattr(config, "AUTH_PASSWORD", test_password)

    # Valid Bearer token
    headers = {"Authorization": f"Bearer {test_password}"}
    res = client.get("/api/settings/general", headers=headers)
    assert res.status_code == 200

    res = client.get("/api/tasks", headers=headers)
    assert res.status_code == 200

    # Auth status endpoint with Bearer token reports authenticated=True
    res = client.get("/api/auth/status", headers=headers)
    assert res.status_code == 200
    status_data = AuthStatusResponse(**res.json())
    assert status_data.protected_mode is True
    assert status_data.authenticated is True

    # Invalid Bearer token
    invalid_headers = {"Authorization": "Bearer wrong_password"}
    res = client.get("/api/settings/general", headers=invalid_headers)
    assert res.status_code == 401
    assert res.json()["code"] == "UNAUTHORIZED"


def test_protected_mode_web_login_and_cookie_flow(client: TestClient, monkeypatch):
    """Test full web login flow: password check, cookie setting, and logout."""
    test_password = "web_user_password_456"
    monkeypatch.setattr(config, "AUTH_PASSWORD", test_password)

    # 1. Login with wrong password
    res = client.post("/api/auth/login", json={"password": "wrong_password"})
    assert res.status_code == 401
    assert res.json()["detail"] == "密码错误"
    assert config.SESSION_COOKIE_NAME not in res.cookies

    # 2. Login with correct password
    res = client.post("/api/auth/login", json={"password": test_password})
    assert res.status_code == 200
    login_resp = LoginResponse(**res.json())
    assert login_resp.success is True
    assert config.SESSION_COOKIE_NAME in res.cookies

    session_cookie = res.cookies[config.SESSION_COOKIE_NAME]
    assert SessionManager.verify_session_token(session_cookie, test_password) is True

    # 3. Access protected endpoint using the returned session cookie
    client.cookies.set(config.SESSION_COOKIE_NAME, session_cookie)

    res = client.get("/api/settings/general")
    assert res.status_code == 200

    res = client.get("/api/tasks")
    assert res.status_code == 200

    # Status check reflects authenticated=True
    res = client.get("/api/auth/status")
    assert res.status_code == 200
    status_data = AuthStatusResponse(**res.json())
    assert status_data.protected_mode is True
    assert status_data.authenticated is True

    # 4. Logout clears the session cookie
    res = client.post("/api/auth/logout")
    assert res.status_code == 200
    assert res.json()["success"] is True

    # Client cookie should be deleted or expired
    client.cookies.clear()
    res = client.get("/api/settings/general")
    assert res.status_code == 401


def test_protected_mode_tampered_cookie(client: TestClient, monkeypatch):
    """A forged or tampered session cookie is rejected with 401."""
    test_password = "test_pass_forgery"
    monkeypatch.setattr(config, "AUTH_PASSWORD", test_password)

    client.cookies.set(config.SESSION_COOKIE_NAME, "forged_payload.forged_signature")
    res = client.get("/api/settings/general")
    assert res.status_code == 401


# ============================================================================
# 4. Rate Limiter & Anti-Brute-Force Tests
# ============================================================================


def test_rate_limiter_unit():
    """Unit test for LoginRateLimiter sliding window and lockout logic."""
    limiter = LoginRateLimiter(max_failures=3, window_seconds=2, lockout_seconds=5)
    test_ip = "192.168.1.100"

    # First two failures do not block
    blocked, _ = limiter.record_failure(test_ip)
    assert blocked is False
    blocked, _ = limiter.is_blocked(test_ip)
    assert blocked is False

    blocked, _ = limiter.record_failure(test_ip)
    assert blocked is False

    # 3rd failure reaches max_failures and triggers lockout
    blocked, remaining = limiter.record_failure(test_ip)
    assert blocked is True
    assert remaining > 0

    blocked, _ = limiter.is_blocked(test_ip)
    assert blocked is True

    # Reset clears lockout
    limiter.reset(test_ip)
    blocked, _ = limiter.is_blocked(test_ip)
    assert blocked is False


def test_brute_force_lockout_endpoint(client: TestClient, monkeypatch):
    """Test that consecutive failed logins from the same client IP trigger HTTP 429."""
    test_password = "admin_password_999"
    monkeypatch.setattr(config, "AUTH_PASSWORD", test_password)

    # Use a custom limiter instance for isolated testing
    custom_limiter = LoginRateLimiter(max_failures=3, window_seconds=60, lockout_seconds=60)
    monkeypatch.setattr("server.routers.auth.rate_limiter", custom_limiter)

    headers = {"X-Forwarded-For": "203.0.113.195"}

    # 1st failure -> 401
    res = client.post("/api/auth/login", json={"password": "bad1"}, headers=headers)
    assert res.status_code == 401

    # 2nd failure -> 401
    res = client.post("/api/auth/login", json={"password": "bad2"}, headers=headers)
    assert res.status_code == 401

    # 3rd failure -> 429 Lockout triggered
    res = client.post("/api/auth/login", json={"password": "bad3"}, headers=headers)
    assert res.status_code == 429
    assert "Retry-After" in res.headers
    assert "锁定" in res.json()["detail"]

    # 4th attempt (even with correct password) while locked -> 429
    res = client.post("/api/auth/login", json={"password": test_password}, headers=headers)
    assert res.status_code == 429

    # Another IP should NOT be blocked
    other_headers = {"X-Forwarded-For": "198.51.100.1"}
    res = client.post("/api/auth/login", json={"password": test_password}, headers=other_headers)
    assert res.status_code == 200
