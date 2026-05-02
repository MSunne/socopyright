"""用户账号的 CRUD 抽象。底层走 SQLite（users 表）。

上层代码（auth / admin_api / main）只依赖这里的函数签名，不直接碰 SQLAlchemy 模型。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from .db import AsyncSessionLocal
from .models import User


def _to_dict(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "password_hash": u.password_hash,
        "is_active": u.is_active,
        "created_at": u.created_at.isoformat() if u.created_at else datetime.utcnow().isoformat(),
    }


async def get_by_username(username: str) -> dict | None:
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(User).where(User.username == username))).scalar_one_or_none()
        return _to_dict(row) if row else None


async def get_by_id(user_id: int) -> dict | None:
    async with AsyncSessionLocal() as s:
        row = await s.get(User, user_id)
        return _to_dict(row) if row else None


async def list_all() -> list[dict]:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(User).order_by(User.id))).scalars().all()
        return [_to_dict(r) for r in rows]


async def create(username: str, password_hash: str) -> dict:
    async with AsyncSessionLocal() as s:
        exists = (await s.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if exists:
            raise ValueError("username exists")
        user = User(username=username, password_hash=password_hash, is_active=True)
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return _to_dict(user)


async def update(user_id: int, *, password_hash: str | None = None, is_active: bool | None = None) -> dict | None:
    async with AsyncSessionLocal() as s:
        user = await s.get(User, user_id)
        if not user:
            return None
        if password_hash is not None:
            user.password_hash = password_hash
        if is_active is not None:
            user.is_active = is_active
        await s.commit()
        await s.refresh(user)
        return _to_dict(user)


async def delete(user_id: int) -> bool:
    async with AsyncSessionLocal() as s:
        user = await s.get(User, user_id)
        if not user:
            return False
        await s.delete(user)
        await s.commit()
        return True
