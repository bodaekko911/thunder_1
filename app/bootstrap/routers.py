from fastapi import FastAPI

from app.routers import ROUTERS


def include_routers(app: FastAPI) -> None:
    for router in ROUTERS:
        app.include_router(router)
