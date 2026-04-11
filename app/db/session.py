from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

import anyio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.base import Base


engine = create_async_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autoflush=False,
    expire_on_commit=False,
)


class QueryProxy:
    _chain_methods = {
        "filter",
        "filter_by",
        "order_by",
        "join",
        "outerjoin",
        "group_by",
        "having",
        "offset",
        "limit",
        "distinct",
        "options",
    }
    _terminal_methods = {
        "all",
        "first",
        "one",
        "one_or_none",
        "scalar",
        "count",
        "delete",
        "update",
        "get",
    }

    def __init__(self, session: "SessionProxy", entities: tuple[Any, ...], ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] | None = None):
        self._session = session
        self._entities = entities
        self._ops = ops or []

    def _clone(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> "QueryProxy":
        return QueryProxy(self._session, self._entities, [*self._ops, (name, args, kwargs)])

    def __getattr__(self, name: str):
        if name in self._chain_methods:
            return lambda *args, **kwargs: self._clone(name, args, kwargs)
        if name in self._terminal_methods:
            return lambda *args, **kwargs: self._session.run_sync(
                self._run_terminal, name, args, kwargs
            )
        raise AttributeError(name)

    def _run_terminal(
        self,
        sync_session,
        terminal: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ):
        query = sync_session.query(*self._entities)
        for name, op_args, op_kwargs in self._ops:
            query = getattr(query, name)(*op_args, **op_kwargs)
        return getattr(query, terminal)(*args, **kwargs)


class SessionProxy:
    def __init__(self, session: AsyncSession):
        self._session = session

    def run_sync(self, fn: Callable[..., Any], *args, **kwargs):
        async def runner() -> Any:
            return await self._session.run_sync(lambda sync_session: fn(sync_session, *args, **kwargs))

        return anyio.from_thread.run(runner)

    def query(self, *entities: Any) -> QueryProxy:
        return QueryProxy(self, entities)

    def add(self, instance: Any) -> None:
        self.run_sync(lambda sync_session: sync_session.add(instance))

    def add_all(self, instances: list[Any]) -> None:
        self.run_sync(lambda sync_session: sync_session.add_all(instances))

    def delete(self, instance: Any) -> None:
        async def runner() -> None:
            await self._session.delete(instance)

        anyio.from_thread.run(runner)

    def commit(self) -> None:
        anyio.from_thread.run(self._session.commit)

    def rollback(self) -> None:
        anyio.from_thread.run(self._session.rollback)

    def refresh(self, instance: Any) -> None:
        anyio.from_thread.run(self._session.refresh, instance)

    def flush(self) -> None:
        anyio.from_thread.run(self._session.flush)

    def close(self) -> None:
        anyio.from_thread.run(self._session.close)

    @property
    def bind(self):
        return self._session.sync_session.bind


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def get_db() -> AsyncGenerator[SessionProxy, None]:
    async with AsyncSessionLocal() as session:
        yield SessionProxy(session)


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
