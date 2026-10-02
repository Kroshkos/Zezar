"""
Движок БД: async SQLAlchemy + SQLite (файл в /data, путь из .env).
SQLite: WAL + FK через event-listener.
"""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.event import listens_for
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=False,
    # StaticPool + check_same_thread — стабильно для одного файла SQLite
    connect_args={"check_same_thread": False, "timeout": 30},
    poolclass=StaticPool,
)


@listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncSession:  # зависимость FastAPI
    async with SessionLocal() as session:
        yield session
