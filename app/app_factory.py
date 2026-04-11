from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.bootstrap import include_routers, initialize_database
from app.core.config import settings


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    include_routers(app)

    @app.get("/health")
    def health():
        return {"status": "ok", "app": settings.APP_NAME}

    return app
