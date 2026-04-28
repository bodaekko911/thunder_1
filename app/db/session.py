from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings


engine = create_async_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=settings.POOL_SIZE,
    max_overflow=settings.POOL_MAX_OVERFLOW,
    pool_timeout=30,
    pool_recycle=1800,
    connect_args={"server_settings": {"application_name": "thunder-erp"}},
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autoflush=False,
    expire_on_commit=False,
)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


# Legacy alias kept for any remaining references — same as get_async_session
get_db = get_async_session


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
