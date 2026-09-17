from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import aiohttp

from .image_utils import PreparedImage
from .models import SearchResponse, parse_search_response


MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_WARMUP_BYTES = 2 * 1024 * 1024


class SoutubotError(RuntimeError):
    """Base exception with a user-safe message."""


class SoutubotBlockedError(SoutubotError):
    pass


class SoutubotRateLimitError(SoutubotError):
    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SoutubotResponseError(SoutubotError):
    pass


@dataclass(frozen=True, slots=True)
class SearchParameters:
    factor: float = 1.2
    metadata_mode: str = "display"
    top_k: int = 25


class SoutubotClient:
    def __init__(
        self,
        *,
        base_url: str = "https://soutubot.moe",
        timeout_seconds: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(10.0, float(timeout_seconds))
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._warm_lock = asyncio.Lock()
        self._warmed = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session and not self._session.closed:
                return self._session
            timeout = aiohttp.ClientTimeout(
                total=None,
                connect=min(30.0, self.timeout_seconds),
                sock_connect=min(30.0, self.timeout_seconds),
                sock_read=self.timeout_seconds,
            )
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                cookie_jar=aiohttp.CookieJar(),
                headers={
                    "User-Agent": "AstrBot-SoutuBotPlugin/0.1",
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Origin": self.base_url,
                    "Referer": self.base_url + "/",
                },
            )
            return self._session

    async def warm_up(self, *, force: bool = False) -> None:
        if self._warmed and not force:
            return
        async with self._warm_lock:
            if self._warmed and not force:
                return
            session = await self._get_session()
            async with session.get(self.base_url + "/") as response:
                await self._read_limited(
                    response,
                    limit=MAX_WARMUP_BYTES,
                    too_large_message="搜图网站主页返回内容过大",
                )
                if response.status in {401, 403}:
                    raise SoutubotBlockedError("搜图网站拒绝了服务器访问（HTTP 403）")
                if response.status >= 400:
                    raise SoutubotResponseError(
                        f"搜图网站主页访问失败（HTTP {response.status}）"
                    )
            self._warmed = True

    @staticmethod
    def _make_form(image: PreparedImage, params: SearchParameters) -> aiohttp.FormData:
        form = aiohttp.FormData()
        form.add_field(
            "file",
            image.data,
            filename=image.filename,
            content_type=image.content_type,
        )
        form.add_field("factor", f"{params.factor:.1f}")
        form.add_field("metadata_mode", params.metadata_mode)
        form.add_field("top_k", str(params.top_k))
        return form

    @staticmethod
    async def _read_limited(
        response: aiohttp.ClientResponse,
        *,
        limit: int,
        too_large_message: str,
    ) -> bytes:
        raw = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            raw.extend(chunk)
            if len(raw) > limit:
                raise SoutubotResponseError(too_large_message)
        return bytes(raw)

    async def _read_payload(self, response: aiohttp.ClientResponse) -> tuple[Any, str]:
        raw = await self._read_limited(
            response,
            limit=MAX_RESPONSE_BYTES,
            too_large_message="搜图服务返回内容过大",
        )
        text = raw.decode(response.charset or "utf-8", errors="replace")
        try:
            return json.loads(text), text
        except json.JSONDecodeError:
            return {}, text

    @staticmethod
    def _detail(payload: Any, fallback: str) -> str:
        if isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("message")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()[:300]
        return fallback

    async def _post_once(
        self, image: PreparedImage, params: SearchParameters
    ) -> SearchResponse:
        session = await self._get_session()
        endpoint = urljoin(self.base_url + "/", "api/search")
        async with session.post(
            endpoint,
            data=self._make_form(image, params),
        ) as response:
            payload, _ = await self._read_payload(response)
            if response.status in {401, 403}:
                raise SoutubotBlockedError("搜图网站拒绝了服务器访问（HTTP 403）")
            if response.status == 429:
                retry_after_text = response.headers.get("Retry-After")
                try:
                    retry_after = int(retry_after_text) if retry_after_text else None
                except ValueError:
                    retry_after = None
                raise SoutubotRateLimitError(
                    self._detail(payload, "搜图请求过于频繁，请稍后再试"),
                    retry_after=retry_after,
                )
            if response.status >= 400:
                raise SoutubotResponseError(
                    self._detail(payload, f"搜图请求失败（HTTP {response.status}）")
                )
            try:
                return parse_search_response(payload, base_url=self.base_url)
            except ValueError as exc:
                raise SoutubotResponseError(str(exc)) from exc

    async def search(
        self, image: PreparedImage, params: SearchParameters
    ) -> SearchResponse:
        async def operation() -> SearchResponse:
            await self.warm_up()
            try:
                return await self._post_once(image, params)
            except SoutubotBlockedError:
                self._warmed = False
                await self.warm_up(force=True)
                return await self._post_once(image, params)

        try:
            return await asyncio.wait_for(operation(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise SoutubotError(
                f"搜图请求超过 {int(self.timeout_seconds)} 秒，已停止等待"
            ) from exc
        except aiohttp.ClientError as exc:
            raise SoutubotError("无法连接搜图网站，请稍后重试") from exc

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._warmed = False
