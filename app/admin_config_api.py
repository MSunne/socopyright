"""/admin/config：查看与修改所有运行期配置。

所有 .env 字段都可通过这里修改：
- 写入 .env（持久化）
- 在 settings 单例上 setattr（代码里每次引用 settings.X 都会拿到新值，自然热生效）
- 对有副作用的字段（LLM 客户端、信号量、worker 池）触发 reload

"secret" 字段（API key 等）GET 时掩码回显，PUT 时留空或等于掩码视为不改。
"restart" 字段（host / port / db）写入 .env 但只有下次重启才真正生效。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import llm, worker
from .auth import require_admin
from .config import settings
from .env_writer import update_env
from .screenshot import capture

router = APIRouter(prefix="/admin/config", tags=["admin"], dependencies=[Depends(require_admin)])

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


# 字段元数据（顺序即页面展示顺序）
# type:  str / int / secret
# group: 页面分组名
# restart: True 表示写入 .env 后需要重启进程才能真正生效
FIELDS: list[dict[str, Any]] = [
    {"name": "LLM_BASE_URL", "type": "str", "group": "大模型", "label": "API Base URL"},
    {"name": "LLM_API_KEY", "type": "secret", "group": "大模型", "label": "API Key", "hint": "留空或保持掩码不变即不修改"},
    {"name": "LLM_MODEL", "type": "str", "group": "大模型", "label": "模型名"},
    {"name": "LLM_MAX_CONCURRENCY", "type": "int", "group": "大模型", "label": "LLM 最大并发", "min": 1, "max": 64},
    {"name": "JOB_CONCURRENCY", "type": "int", "group": "并发", "label": "Job 并发", "min": 1, "max": 32, "hint": "只能扩容；缩容需重启"},
    {"name": "BROWSER_MAX_CONCURRENCY", "type": "int", "group": "并发", "label": "Playwright 并发", "min": 1, "max": 32, "hint": "每实例约占 100-200MB 内存"},
    {"name": "ADMIN_TOKEN", "type": "secret", "group": "鉴权", "label": "Admin Token", "hint": "改完后本页面自动切换到新 token"},
    {"name": "JWT_SECRET", "type": "secret", "group": "鉴权", "label": "JWT Secret", "hint": "改动会使所有已登录用户被迫重新登录"},
    {"name": "JWT_ALGORITHM", "type": "str", "group": "鉴权", "label": "JWT 算法", "hint": "一般保持 HS256"},
    {"name": "JWT_EXPIRE_HOURS", "type": "int", "group": "鉴权", "label": "JWT 有效期（小时）", "min": 1, "max": 8760},
    {"name": "APP_HOST", "type": "str", "group": "服务器", "label": "监听 Host", "restart": True},
    {"name": "APP_PORT", "type": "int", "group": "服务器", "label": "监听端口", "min": 1, "max": 65535, "restart": True},
    {"name": "DATABASE_URL", "type": "str", "group": "服务器", "label": "数据库 URL", "restart": True, "hint": "SQLite 或 postgresql+asyncpg"},
    {"name": "DATA_DIR", "type": "str", "group": "服务器", "label": "产物目录", "hint": "新 job 写入新路径，旧 job 仍留在原路径"},
]

_BY_NAME = {f["name"]: f for f in FIELDS}
_SECRET = {f["name"] for f in FIELDS if f["type"] == "secret"}
_INT = {f["name"] for f in FIELDS if f["type"] == "int"}
_RESTART = {f["name"] for f in FIELDS if f.get("restart")}


def _mask(v: str) -> str:
    if not v:
        return ""
    if len(v) <= 8:
        return "****"
    return v[:4] + "****" + v[-4:]


def _display_value(name: str) -> Any:
    v = getattr(settings, name)
    if name in _SECRET:
        return _mask(v or "")
    return v


class ConfigUpdate(BaseModel):
    LLM_BASE_URL: str | None = None
    LLM_API_KEY: str | None = None
    LLM_MODEL: str | None = None
    LLM_MAX_CONCURRENCY: int | None = Field(default=None, ge=1, le=64)
    JOB_CONCURRENCY: int | None = Field(default=None, ge=1, le=32)
    BROWSER_MAX_CONCURRENCY: int | None = Field(default=None, ge=1, le=32)
    ADMIN_TOKEN: str | None = None
    JWT_SECRET: str | None = None
    JWT_ALGORITHM: str | None = None
    JWT_EXPIRE_HOURS: int | None = Field(default=None, ge=1, le=8760)
    APP_HOST: str | None = None
    APP_PORT: int | None = Field(default=None, ge=1, le=65535)
    DATABASE_URL: str | None = None
    DATA_DIR: str | None = None


@router.get("")
async def get_config() -> dict[str, Any]:
    return {
        "fields": [{**meta, "value": _display_value(meta["name"])} for meta in FIELDS],
    }


@router.put("")
async def put_config(body: ConfigUpdate) -> dict[str, Any]:
    changes: dict[str, str] = {}
    applied: dict[str, Any] = {}
    notes: list[str] = []

    submitted = body.model_dump(exclude_none=True)

    for name, value in submitted.items():
        meta = _BY_NAME.get(name)
        if meta is None:
            continue
        if name in _SECRET:
            v = str(value).strip()
            # 留空或等于当前掩码视为不改
            if not v or v == _mask(getattr(settings, name) or ""):
                continue
            changes[name] = v
            applied[name] = _mask(v)
        elif name in _INT:
            changes[name] = str(value)
            applied[name] = int(value)
        else:
            v = str(value)
            # 如果值没变，跳过（减少 .env 无意义重写）
            if v == str(getattr(settings, name)):
                continue
            changes[name] = v
            applied[name] = v

    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有需要保存的改动")

    # 1. 写 .env
    update_env(_ENV_PATH, changes)

    # 2. 内存 settings 同步
    for k, v in changes.items():
        typed: Any = int(v) if k in _INT else v
        setattr(settings, k, typed)

    # 3. 热更新副作用
    if changes.keys() & {"LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_MAX_CONCURRENCY"}:
        llm.reload_runtime()
    if "BROWSER_MAX_CONCURRENCY" in changes:
        capture.reload_runtime()
    if "JOB_CONCURRENCY" in changes:
        active = worker.reconcile_workers()
        if active > settings.JOB_CONCURRENCY:
            notes.append(f"Worker 当前 {active} 个，大于新目标 {settings.JOB_CONCURRENCY}，缩容需重启进程")
    if "JWT_SECRET" in changes:
        notes.append("JWT_SECRET 已更新，所有已登录用户（非 admin）的 token 已失效，需重新登录")
    if "DATA_DIR" in changes:
        notes.append("DATA_DIR 已更新，仅对新 job 生效；旧 job 的产物仍在原路径")

    restart_touched = sorted(changes.keys() & _RESTART)
    if restart_touched:
        notes.append(f"以下字段已写入 .env，但需要重启进程才会真正生效：{', '.join(restart_touched)}")

    return {"ok": True, "applied": applied, "notes": notes}
