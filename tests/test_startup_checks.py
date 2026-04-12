from fastapi.testclient import TestClient

from app.app_factory import create_app
from app.database import get_async_session


class UnavailableSession:
    async def execute(self, _query):
        raise RuntimeError("database unavailable")


def test_lifespan_runs_startup_hooks(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_verify_migration_status() -> None:
        calls.append("migrations")

    monkeypatch.setattr("app.app_factory.configure_logging", lambda: calls.append("logging"))
    monkeypatch.setattr("app.app_factory.configure_monitoring", lambda: calls.append("monitoring"))
    monkeypatch.setattr("app.app_factory.verify_migration_status", fake_verify_migration_status)

    app = create_app()

    with TestClient(app) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert calls == ["logging", "monitoring", "migrations"]


def test_readiness_endpoint_returns_503_when_db_is_unavailable(monkeypatch) -> None:
    async def noop() -> None:
        return None

    async def override_session():
        yield UnavailableSession()

    monkeypatch.setattr("app.app_factory.configure_logging", lambda: None)
    monkeypatch.setattr("app.app_factory.configure_monitoring", lambda: None)
    monkeypatch.setattr("app.app_factory.verify_migration_status", noop)

    app = create_app()
    app.dependency_overrides[get_async_session] = override_session

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "db": "unreachable"}
