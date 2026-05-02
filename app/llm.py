"""OpenAI 兼容 LLM 客户端封装。

- 全局 Semaphore 限并发，避免打爆上游
- call_text: 纯文本输出
- call_json: 带重试的 JSON 解析，出错时把错误回传让模型修复
- 通过 .env 的 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 配置
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from openai import AsyncOpenAI

from .config import settings

logger = logging.getLogger(__name__)

_sem = asyncio.Semaphore(settings.LLM_MAX_CONCURRENCY)
_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(base_url=settings.LLM_BASE_URL, api_key=settings.LLM_API_KEY)
    return _client


def reload_runtime() -> None:
    """LLM 配置变更后调用：重置 client 和并发信号量，让后续调用采用新值。

    对已经在 `async with _sem:` 里持有令牌的请求无影响；新请求按新上限排队。
    """
    global _client, _sem
    _client = None
    _sem = asyncio.Semaphore(max(1, settings.LLM_MAX_CONCURRENCY))


async def call_text(
    prompt: str,
    *,
    system: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    model: str | None = None,
) -> str:
    """普通文本生成。"""
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    async with _sem:
        resp = await get_client().chat.completions.create(
            model=model or settings.LLM_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    content = resp.choices[0].message.content or ""
    return content.strip()


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> str:
    """从可能带 markdown 栅栏或额外文本的 LLM 输出里抠出 JSON 字符串。"""
    text = text.strip()
    m = _JSON_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    m = _JSON_OBJECT_RE.search(text)
    if m:
        return m.group(0).strip()
    return text


async def call_json(
    prompt: str,
    *,
    system: str | None = None,
    temperature: float = 0.6,
    max_retries: int = 3,
    model: str | None = None,
    expect_json_mode: bool = True,
) -> dict:
    """调用 LLM 并解析成 JSON，失败自动重试（把错误原因反馈回模型）。"""
    base_system = system or "你是一个严谨的结构化输出助手。必须只返回合法 JSON，不要任何解释或 markdown 包装。"

    last_err: str | None = None
    last_raw: str | None = None
    for attempt in range(1, max_retries + 1):
        user_content = prompt
        if last_err:
            user_content = (
                f"上一次你的输出不是合法 JSON，错误是：{last_err}\n"
                f"上一次输出片段：{(last_raw or '')[:500]}\n\n"
                f"请重新输出，只返回合法 JSON，没有任何其他文字：\n\n{prompt}"
            )

        messages = [
            {"role": "system", "content": base_system},
            {"role": "user", "content": user_content},
        ]
        create_kwargs: dict[str, Any] = {
            "model": model or settings.LLM_MODEL,
            "messages": messages,
            "temperature": temperature,
        }
        if expect_json_mode:
            create_kwargs["response_format"] = {"type": "json_object"}

        async with _sem:
            try:
                resp = await get_client().chat.completions.create(**create_kwargs)
            except Exception as e:
                # 某些兼容 endpoint 不支持 response_format，移除重试一次
                if expect_json_mode and "response_format" in str(e):
                    create_kwargs.pop("response_format", None)
                    resp = await get_client().chat.completions.create(**create_kwargs)
                else:
                    raise

        raw = (resp.choices[0].message.content or "").strip()
        last_raw = raw
        try:
            return json.loads(_extract_json(raw))
        except json.JSONDecodeError as e:
            last_err = f"{type(e).__name__}: {e.msg} (pos {e.pos})"
            logger.warning("LLM JSON parse failed (attempt %d/%d): %s", attempt, max_retries, last_err)

    raise ValueError(f"LLM 连续 {max_retries} 次返回非法 JSON: {last_err}; raw={last_raw!r}")
