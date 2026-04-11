from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.bootstrap import include_routers, initialize_database
from app.core.config import settings

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    (STATIC_DIR / "uploads").mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    include_routers(app)

    @app.get("/health")
    def health():
        return {"status": "ok", "app": settings.APP_NAME}

    return app
