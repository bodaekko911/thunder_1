import os
from collections.abc import AsyncGenerator

import pytest
from fastapi.testclient import TestClient

os.environ["ADMIN_PASSWORD"] = "change-me-now"

from app.app_factory import create_app
from app.database import get_async_session


class FakeReadySession:
    async def execute(self, _query):
        return 1


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def noop() -> None:
        return None

    async def override_session() -> AsyncGenerator[FakeReadySession, None]:
        yield FakeReadySession()

    monkeypatch.setattr("app.app_factory.configure_logging", lambda: None)
    monkeypatch.setattr("app.app_factory.configure_monitoring", lambda: None)
    monkeypatch.setattr("app.app_factory.verify_migration_status", noop)

    app = create_app()
    app.dependency_overrides[get_async_session] = override_session

    with TestClient(app) as test_client:
        yield test_client
