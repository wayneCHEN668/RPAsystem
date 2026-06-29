"""
data/api_client.py
-------------------
通过 REST API 获取业务数据。

职责：
  - 发送 GET / POST 请求，返回解析后的 dict / list
  - 支持 Bearer Token / Basic Auth / 自定义 Header
  - 统一重试（网络抖动）与超时处理
  - 响应缓存（同一请求在单次流程内只发一次）

无业务逻辑，只负责 HTTP 通信。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


class ApiClient:
    """
    异步 REST API 客户端，带重试和简单内存缓存。

    用法：
        async with ApiClient(base_url="https://api.example.com",
                             token="Bearer sk-xxx") as client:
            data = await client.get("orders", params={"status": "pending"})
    """

    def __init__(
        self,
        base_url: str = "",
        token: str = "",
        username: str = "",
        password: str = "",
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        enable_cache: bool = True,
    ):
        self.base_url   = base_url.rstrip("/")
        self.timeout    = timeout
        self.enable_cache = enable_cache
        self._cache: dict[str, Any] = {}
        self._client: httpx.AsyncClient | None = None

        # 构造请求头
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if token:
            # 支持 "Bearer xxx" 或直接传 token
            self._headers["Authorization"] = (
                token if token.lower().startswith("bearer ") else f"Bearer {token}"
            )
        if headers:
            self._headers.update(headers)

        # Basic Auth
        self._auth: httpx.BasicAuth | None = (
            httpx.BasicAuth(username, password) if username else None
        )

    # ── Context Manager ───────────────────────

    async def __aenter__(self) -> ApiClient:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers,
            auth=self._auth,
            timeout=self.timeout,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()

    # ── 公开接口 ──────────────────────────────

    async def get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """
        发送 GET 请求，返回解析后的 JSON（dict 或 list）。

        Parameters
        ----------
        endpoint : str
            相对路径，如 "orders" 或 "/orders/pending"。
        params : dict
            URL 查询参数。
        """
        cache_key = self._cache_key("GET", endpoint, params or {})
        if self.enable_cache and cache_key in self._cache:
            logger.debug(f"[api] cache hit: GET {endpoint}")
            return self._cache[cache_key]

        result = await self._get(endpoint, params or {})
        if self.enable_cache:
            self._cache[cache_key] = result
        return result

    async def post(
        self,
        endpoint: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """发送 POST 请求，返回解析后的 JSON。"""
        return await self._post(endpoint, body or {})

    def clear_cache(self) -> None:
        """清除响应缓存（长流程中途需要刷新数据时调用）。"""
        self._cache.clear()

    # ── 带重试的内部实现 ──────────────────────

    @retry(
        retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _get(self, endpoint: str, params: dict) -> Any:
        assert self._client, "请在 async with 块内使用 ApiClient"
        url = endpoint if endpoint.startswith("http") else endpoint.lstrip("/")
        logger.debug(f"[api] GET {url} params={params}")
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    @retry(
        retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _post(self, endpoint: str, body: dict) -> Any:
        assert self._client, "请在 async with 块内使用 ApiClient"
        url = endpoint if endpoint.startswith("http") else endpoint.lstrip("/")
        logger.debug(f"[api] POST {url} body={json.dumps(body)[:80]}")
        resp = await self._client.post(url, json=body)
        resp.raise_for_status()
        return resp.json()

    # ── 工具 ──────────────────────────────────

    @staticmethod
    def _cache_key(method: str, endpoint: str, params: dict) -> str:
        raw = f"{method}:{endpoint}:{json.dumps(params, sort_keys=True)}"
        return hashlib.md5(raw.encode()).hexdigest()
