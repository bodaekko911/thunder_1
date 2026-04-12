from fastapi.testclient import TestClient


def test_liveness_endpoint(client: TestClient) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["app"]
    assert payload["environment"] in {"development", "production"}


def test_readiness_endpoint_returns_ok_when_db_is_available(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}
