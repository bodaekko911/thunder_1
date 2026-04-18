"""
test_auth_session_ux.py

Verifies the two-part session-expiry fix:

  • Server-side middleware (app_factory._session_expiry):
    – HTML GET + 401 → 307 to /?next=<path>&reason=expired
    – JSON GET + 401 → plain 401 JSON (API clients unaffected)
    – HTML GET + 401 + valid refresh cookie → 307 back to same path with
      a new access_token cookie (silent refresh)
    – POST + 401 → never rewritten (method guard)

  • /auth/* exclusion:
    – POST /auth/login with bad credentials → JSON 401, no redirect

The tests use monkeypatching for two things that would require a real DB:
  - app.app_factory.verify_migration_status  (startup hook)
  - app.app_factory._try_silent_refresh      (DB + token logic in middleware)
"""

from collections.abc import AsyncGenerator

import pytest
from fastapi.testclient import TestClient

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.app_factory as app_factory_module
from app.app_factory import create_app
from app.database import get_async_session


# ── Minimal fake DB session ───────────────────────────────────────────────────

class _FakeSession:
    """Satisfies the health-check readiness query; auth queries are handled
    by dependency overrides or monkeypatching at a higher level."""

    async def execute(self, _query):
        class _R:
            def scalar_one_or_none(self):
                return None
        return _R()


# ── Shared fixture ────────────────────────────────────────────────────────────

@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def _noop() -> None:
        return None

    async def _override_session() -> AsyncGenerator:
        yield _FakeSession()

    monkeypatch.setattr("app.app_factory.configure_logging", lambda: None)
    monkeypatch.setattr("app.app_factory.configure_monitoring", lambda: None)
    monkeypatch.setattr("app.app_factory.verify_migration_status", _noop)

    app = create_app()
    app.dependency_overrides[get_async_session] = _override_session

    # TestClient must NOT follow redirects so we can assert the 307 itself.
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_html_get_no_auth_redirects_to_login_with_next_and_reason(client: TestClient) -> None:
    """GET /dashboard + Accept: text/html + no cookies → 307 to /?next=…&reason=expired."""
    response = client.get(
        "/dashboard",
        headers={"Accept": "text/html,application/xhtml+xml"},
        allow_redirects=False,
    )
    assert response.status_code == 307
    location = response.headers["location"]
    assert "next=%2Fdashboard" in location
    assert "reason=expired" in location
    assert location.startswith("/?")


def test_json_get_no_auth_returns_401_json(client: TestClient) -> None:
    """GET /dashboard + Accept: application/json + no cookies → 401 JSON (not a redirect)."""
    response = client.get(
        "/dashboard",
        headers={"Accept": "application/json"},
        allow_redirects=False,
    )
    assert response.status_code == 401
    body = response.json()
    assert "detail" in body


def test_html_get_valid_refresh_cookie_triggers_silent_refresh(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    GET /dashboard + Accept: text/html + refresh_token cookie + silent refresh
    succeeds → 307 back to /dashboard with new access_token cookie set.
    """
    async def _mock_refresh(token_value: str):
        assert token_value == "valid-refresh-token"
        return "new-access-token-xyz"

    monkeypatch.setattr("app.app_factory._try_silent_refresh", _mock_refresh)

    response = client.get(
        "/dashboard",
        headers={"Accept": "text/html,application/xhtml+xml"},
        cookies={"refresh_token": "valid-refresh-token"},
        allow_redirects=False,
    )
    assert response.status_code == 307
    # Should redirect back to the original path, not to the login page.
    assert response.headers["location"] == "/dashboard"
    # New access_token cookie must be present in the redirect response.
    set_cookie = response.headers.get("set-cookie", "")
    assert "access_token" in set_cookie


def test_post_no_auth_returns_non_redirect(client: TestClient) -> None:
    """POST + no cookies → middleware does NOT rewrite (method guard); still gets 4xx, not 307."""
    response = client.post(
        "/users/api/change-password",
        json={
            "old_password": "irrelevant",
            "new_password": "irrelevant2",
            "confirm_new_password": "irrelevant2",
        },
        headers={"Accept": "text/html,application/xhtml+xml"},
        allow_redirects=False,
    )
    assert response.status_code != 307


def test_auth_login_bad_credentials_returns_json_not_redirect(client: TestClient) -> None:
    """POST /auth/login bad credentials → JSON 401 body; /auth/* path is excluded from redirect."""
    response = client.post(
        "/auth/login",
        json={"email": "nobody@example.com", "password": "wrongpassword"},
        headers={"Accept": "text/html,application/xhtml+xml"},
        allow_redirects=False,
    )
    # Redis brute-force check will fail gracefully in tests; the endpoint
    # should still return 401 (or possibly 429/500 if Redis is completely
    # unavailable), but crucially it must NOT return a 307 HTML redirect.
    assert response.status_code != 307
    if response.status_code == 401:
        body = response.json()
        assert "detail" in body
        # Confirm the /auth/* exclusion: no Location header pointing to login page
        assert "location" not in response.headers
