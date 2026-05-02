"""Playwright 截图引擎。

- 全进程共享单个 Browser 实例，每次截图开独立 BrowserContext + Page（隔离状态、可并发）
- 通过 Semaphore 限制同时开的 Page 数
- 入口接受已渲染好的 HTML 字符串，直接 setContent，不写临时文件
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from playwright.async_api import Browser, async_playwright

from ..config import settings

logger = logging.getLogger(__name__)

_browser: Browser | None = None
_playwright: Any = None
_lock = asyncio.Lock()
_sem = asyncio.Semaphore(max(1, settings.BROWSER_MAX_CONCURRENCY))


def reload_runtime() -> None:
    """BROWSER_MAX_CONCURRENCY 变更后调用：替换信号量，下一次截图按新上限排队。"""
    global _sem
    _sem = asyncio.Semaphore(max(1, settings.BROWSER_MAX_CONCURRENCY))


async def get_browser() -> Browser:
    """全局单例 Chromium。"""
    global _browser, _playwright
    async with _lock:
        if _browser is None or not _browser.is_connected():
            _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    return _browser


async def shutdown_browser() -> None:
    global _browser, _playwright
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None


async def capture_html(
    html: str,
    *,
    viewport_width: int = 1280,
    viewport_height: int = 800,
    full_page: bool = False,
    wait_ms: int = 150,
) -> bytes:
    """把 HTML 字符串渲染 → 截图 → 返回 PNG bytes。"""
    async with _sem:
        browser = await get_browser()
        context = await browser.new_context(
            viewport={"width": viewport_width, "height": viewport_height},
            device_scale_factor=2,  # 2x 提高清晰度
        )
        page = await context.new_page()
        try:
            await page.set_content(html, wait_until="domcontentloaded")
            # 等字体/布局稳定
            await page.wait_for_timeout(wait_ms)
            png = await page.screenshot(type="png", full_page=full_page)
            return png
        finally:
            await context.close()


async def html_to_pdf(
    html: str,
    *,
    header_template: str = "",
    footer_template: str = "",
    margin: dict[str, str] | None = None,
    wait_ms: int = 300,
) -> bytes:
    """用 Chromium 把 HTML 渲染成 PDF 字节流（不走 WeasyPrint，省掉原生依赖）。"""
    if margin is None:
        margin = {"top": "22mm", "bottom": "18mm", "left": "18mm", "right": "18mm"}
    async with _sem:
        browser = await get_browser()
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.set_content(html, wait_until="domcontentloaded")
            await page.emulate_media(media="print")
            await page.wait_for_timeout(wait_ms)
            kwargs: dict[str, Any] = {
                "format": "A4",
                "print_background": True,
                "margin": margin,
            }
            if header_template or footer_template:
                kwargs["display_header_footer"] = True
                kwargs["header_template"] = header_template or "<span></span>"
                kwargs["footer_template"] = footer_template or "<span></span>"
            return await page.pdf(**kwargs)
        finally:
            await context.close()
