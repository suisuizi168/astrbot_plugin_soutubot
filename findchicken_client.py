"""Independent SauceNAO transport for find-chicken requests."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

import aiohttp


class FindChickenError(RuntimeError):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class FindChickenClient:
    def __init__(self, api_key: str, proxy: str, timeout: int, limit: int):
        self.api_key = api_key.strip()
        self.proxy = proxy.strip()
        self.timeout = timeout
        self.limit = min(30, max(6, limit * 3))
        self._session = None

    async def search(self, *, image=None, url: str = "") -> dict:
        """Upload prepared bytes or submit a user-provided HTTP image URL.

        Args:
            image: PreparedImage for uploaded message images.
            url: HTTP(S) image URL passed to SauceNAO, not fetched locally.

        Returns:
            Parsed JSON response.

        Raises:
            FindChickenError: A bounded, credential-free failure category.
        """
        if not self.api_key:
            raise FindChickenError("missing_key")
        params = {
            "output_type": "2",
            "db": "999",
            "hide": "0",
            "api_key": self.api_key,
            "numres": str(self.limit),
        }
        form = None
        if url:
            try:
                parsed = urlsplit(url)
                valid = (
                    parsed.scheme in {"http", "https"}
                    and parsed.hostname
                    and not parsed.username
                    and not parsed.password
                )
            except ValueError:
                valid = False
            if not valid or len(url) > 8192:
                raise FindChickenError("image")
            params["url"] = url
        elif image is not None and image.data:
            form = aiohttp.FormData()
            form.add_field(
                "file",
                image.data,
                filename=image.filename,
                content_type=image.content_type,
            )
        else:
            raise FindChickenError("image")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                trust_env=False,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            )
        try:
            async with self._session.post(
                "https://saucenao.com/search.php",
                params=params,
                data=form,
                proxy=self.proxy or None,
                allow_redirects=False,
            ) as response:
                chunks = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    chunks.extend(chunk)
                    if len(chunks) > 2 * 1024 * 1024:
                        raise FindChickenError("response")
                body = chunks.decode("utf-8", errors="replace")
                status = response.status
        except asyncio.TimeoutError as exc:
            raise FindChickenError("timeout") from exc
        except aiohttp.ClientError as exc:
            raise FindChickenError("network") from exc
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        header = payload.get("header", {}) if isinstance(payload, dict) else {}
        header = header if isinstance(header, dict) else {}
        message = str(header.get("message", "")).lower()
        code = str(header.get("status", "0"))
        if code == "-1" or "api key" in message or "anonymous" in message:
            raise FindChickenError("key")
        if status == 429:
            raise FindChickenError("rate_limit")
        if (
            status != 200
            or not isinstance(payload, dict)
            or not isinstance(payload.get("results"), list)
        ):
            raise FindChickenError("response")
        if code != "0" and not payload["results"]:
            raise FindChickenError("response")
        return payload

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None
