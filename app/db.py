import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.DATABASE_URL, echo=False, future=True)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# 简易在线迁移：给已有表加新列（SQLite / PostgreSQL 都支持 ADD COLUMN IF NOT EXISTS 语义的变体）
# SQL 里的 bool 字面量不同方言不一致：PG 用 FALSE，SQLite 用 0
_BOOL_FALSE = {"sqlite": "0", "postgresql": "FALSE"}


def _migrations_for(dialect: str) -> list[tuple[str, str, str]]:
    b_false = _BOOL_FALSE.get(dialect, "FALSE")
    return [
        # (table, column, type_default_clause)
        ("jobs", "template", "VARCHAR(16) NOT NULL DEFAULT 'basic'"),
        ("job_files", "progress", "INTEGER NOT NULL DEFAULT 0"),
        ("jobs", "is_deleted", f"BOOLEAN NOT NULL DEFAULT {b_false}"),
        ("jobs", "started_at", "DATETIME"),  # nullable；用于"纯处理耗时"（排除排队/中断等待）
        ("jobs", "owner_kind", "VARCHAR(16) NOT NULL DEFAULT 'company'"),
    ]


async def _apply_migrations(conn) -> None:
    dialect = conn.dialect.name  # 'sqlite' / 'postgresql'
    for table, col, spec in _migrations_for(dialect):
        # 检查列是否存在
        if dialect == "sqlite":
            rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).all()
            exists = any(r[1] == col for r in rows)
        else:  # postgresql
            exists = (await conn.execute(text(
                "SELECT 1 FROM information_schema.columns WHERE table_name=:t AND column_name=:c"
            ), {"t": table, "c": col})).first() is not None

        if not exists:
            logger.info("迁移：ALTER TABLE %s ADD COLUMN %s %s", table, col, spec)
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {spec}"))


async def init_db() -> None:
    from . import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _apply_migrations(conn)


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
