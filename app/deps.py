"""Общие зависимости FastAPI (ядро)."""
from __future__ import annotations

from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user  # re-export для роутеров
from app.db.engine import SessionLocal


async def session_dep() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
