# engine/browser.py
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from loguru import logger
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
)
from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import settings


class BrowserSession:
    """
    单次自动化会话的浏览器封装。
    用 async context manager 使用：async with BrowserSession() as session:
    """

    def __init__(self, headless: bool | None = None, slow_mo: int | None = None):
        self.headless = headless if headless is not None else settings.headless
        self.slow_mo = slow_mo if slow_mo is not None else settings.slow_mo
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> BrowserSession:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        # 拦截控制台错误，写入日志
        self._context.on("page", self._attach_page_listeners)
        logger.info(f"浏览器会话已启动 headless={self.headless}")
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("浏览器会话已关闭")

    def _attach_page_listeners(self, page: Page) -> None:
        page.on("console", lambda msg: logger.debug(f"[browser] {msg.type}: {msg.text}"))
        page.on("pageerror", lambda err: logger.warning(f"[browser] JS error: {err}"))

    async def new_page(self) -> Page:
        assert self._context, "BrowserSession 未启动，请使用 async with"
        page = await self._context.new_page()
        return page

    # ------------------------------------------------------------------ #
    # 带重试的高级操作
    # ------------------------------------------------------------------ #

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    async def safe_goto(self, page: Page, url: str) -> None:
        """导航到 URL，失败自动重试"""
        logger.debug(f"导航至: {url}")
        await page.goto(url, wait_until="networkidle", timeout=30_000)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=4))
    async def safe_click(self, page: Page, selector: str, timeout: int = 10_000) -> None:
        """点击元素，失败自动重试"""
        logger.debug(f"点击: {selector}")
        await page.locator(selector).click(timeout=timeout)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=4))
    async def safe_fill(self, page: Page, selector: str, value: str) -> None:
        """填写输入框，失败自动重试"""
        logger.debug(f"填写 {selector} = '{value[:20]}...' " if len(value) > 20 else f"填写 {selector} = '{value}'")
        locator = page.locator(selector)
        await locator.clear()
        await locator.fill(value)

    async def screenshot(self, page: Page, name: str) -> str:
        """截图并保存，返回文件路径"""
        screenshots_dir = Path("./reports/screenshots")
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        path = str(screenshots_dir / f"{name}.png")
        await page.screenshot(path=path, full_page=True)
        logger.debug(f"截图已保存: {path}")
        return path

    async def screenshot_as_base64(self, page: Page) -> str:
        """截图并返回 base64，用于报告嵌入"""
        data = await page.screenshot(full_page=False)
        return base64.b64encode(data).decode()

    async def wait_for_stable(self, page: Page, timeout: int = 5_000) -> None:
        """等待页面网络静止（无 pending 请求）"""
        await page.wait_for_load_state("networkidle", timeout=timeout)