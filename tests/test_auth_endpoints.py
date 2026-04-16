from fastapi.testclient import TestClient


def test_login_page_renders_valid_escaped_newline_checks(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert 'url.indexOf("\\r") === -1' in response.text
    assert 'url.indexOf("\\n") === -1' in response.text


def test_auth_me_requires_authentication(client: TestClient) -> None:
    response = client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated"


def test_refresh_requires_refresh_cookie(client: TestClient) -> None:
    response = client.post("/auth/refresh")

    assert response.status_code == 401
    assert response.json()["detail"] == "No refresh token"
