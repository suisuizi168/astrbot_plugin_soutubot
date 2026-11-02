"""SerpAPI Google Lens transport, normalized results and bounded shared cache."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from itertools import zip_longest
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

from .engines.formatter import SearchResult, SourceOutcome
from .image_utils import ImagePreparationError, prepare_image_async


logger = logging.getLogger("astrbot")
MAX_CANDIDATES = 60


def source_priority(row: SearchResult) -> int:
    """Order source types without equating platform with authorship."""
    # Local import avoids a cycle: platform parsers use source_url above.
    from .author_platforms import publisher_identity

    identity = publisher_identity(row.url, row.title)
    if identity.platform == "X (Twitter)" and identity.account:
        return 0
    return 1 if identity.platform else 2


def select_results(
    outcome: SourceOutcome, limit: int, for_authors: bool
) -> SourceOutcome:
    """Keep X/known-platform candidates in the bounded author pool, retaining ranks."""
    result = copy.deepcopy(outcome)
    for rank, row in enumerate(result.results, 1):
        row.extra["search_rank"] = rank
    if for_authors:
        result.results.sort(key=source_priority)
    result.results = result.results[:limit]
    return result


def merge_modes(primary: SourceOutcome, secondary: SourceOutcome) -> SourceOutcome:
    """Interleave mode rankings so later candidates can reach the model."""
    results, seen = [], set()
    for pair in zip_longest(primary.results, secondary.results):
        for row in pair:
            if row is None:
                continue
            key = source_key(row.url)
            if key in seen:
                continue
            seen.add(key)
            results.append(row)
    return SourceOutcome(
        results=results,
        meta={
            "expanded": True,
            "mode_counts": [len(primary.results), len(secondary.results)],
        },
    )


class GoogleLensError(RuntimeError):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def source_url(value) -> str:
    """Accept a bounded HTTP address without embedded credentials.

    Args:
        value: Untrusted URL returned by a provider or supplied as input.

    Returns:
        The stripped URL, or an empty string for invalid input.
    """
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if len(value) > 8192 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme in {"https", "http"}
            and parsed.hostname
            and parsed.username is None
        ):
            _ = parsed.port
            return value
    except ValueError:
        pass
    return ""


def source_key(url: str) -> str:
    """Normalize known URL aliases for conservative result deduplication.

    Args:
        url: A candidate source-page URL.

    Returns:
        An identity key retaining unknown query parameters and fragments.
    """
    if not source_url(url):
        return ""
    parsed = urlsplit(url)
    host = parsed.netloc.lower()
    if host in {"twitter.com", "www.twitter.com", "x.com", "www.x.com"}:
        host = "x.com"
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
        ]
    )
    return urlunsplit(
        (parsed.scheme.lower(), host, parsed.path, query, parsed.fragment)
    )


def parse_lens_results(payload: dict, search_type: str, limit: int) -> SourceOutcome:
    """Normalize Lens candidates without inventing authors or similarity scores.

    Args:
        payload: A successful SerpAPI JSON response.
        search_type: Configured all, visual_matches or exact_matches mode.
        limit: Maximum number of unique candidates to retain.

    Returns:
        Source results with explicit match type and no numeric score.
    """
    keys = (
        ("exact_matches", "visual_matches", "organic_results")
        if search_type == "all"
        else ("exact_matches" if search_type == "exact_matches" else "visual_matches",)
    )
    metadata = payload.get("search_metadata", {})
    status = metadata.get("status") if isinstance(metadata, dict) else None
    if status not in {None, "Success"} or (
        not any(key in payload for key in keys) and status != "Success"
    ):
        raise GoogleLensError("response")
    counts = {}
    rows = []
    for key in keys:
        group = payload.get(key, [])
        if not isinstance(group, list):
            raise GoogleLensError("response")
        counts[key] = len(group)
        rows.extend((key, row) for row in group)
    results, seen = [], set()
    invalid = 0
    for key, row in rows:
        if not isinstance(row, dict):
            invalid += 1
            continue
        url = source_url(row.get("link"))
        title = row.get("title")
        if not url:
            invalid += 1
            continue
        title = title if isinstance(title, str) and title.strip() else "（无标题）"
        identity = source_key(url)
        if identity in seen:
            continue
        seen.add(identity)
        site = row.get("source")
        site = (
            site if isinstance(site, str) and site.strip() else urlsplit(url).hostname
        )
        results.append(
            SearchResult(
                title=" ".join(title.split())[:500],
                source=" ".join(site.split())[:100],
                url=url,
                extra={
                    "provider": "Google Lens",
                    "query_mode": search_type,
                    "match_type": {
                        "exact_matches": "同图匹配",
                        "visual_matches": "视觉相关",
                        "organic_results": "网页相关",
                    }[key],
                },
            )
        )
        if len(results) >= limit:
            break
    return SourceOutcome(
        results=results, meta={"raw_counts": counts, "invalid_rows": invalid}
    )


class GoogleLensClient:
    def __init__(self, config):
        self.api_key = str(config.get("google_lens_api_key", "") or "").strip()
        self.proxy = str(config.get("google_lens_proxy", "") or "").strip()
        self.search_enabled = bool(config.get("google_lens_search_enabled", True))
        self.findchicken_enabled = bool(
            config.get("google_lens_findchicken_enabled", True)
        )
        self.search_type = str(config.get("google_lens_search_type", "all"))
        if self.search_type not in {"all", "visual_matches", "exact_matches"}:
            self.search_type = "all"
        self.retry_empty = bool(config.get("google_lens_retry_empty", True))
        self.expand_sources = bool(config.get("google_lens_expand_sources", True))
        for attr, key, default, low, high in (
            ("timeout", "google_lens_timeout_seconds", 30, 5, 120),
            ("max_results", "google_lens_max_results", 3, 1, 10),
            ("cache_ttl", "google_lens_cache_ttl", 3600, 0, 86400),
        ):
            try:
                value = int(config.get(key, default))
            except (ValueError, TypeError, OverflowError):
                value = default
            setattr(self, attr, min(high, max(low, value)))
        self._session = None
        self._cache = OrderedDict()
        self._inflight = {}
        self._limit = asyncio.Semaphore(2)
        self._closed = False

    async def search(
        self,
        image_bytes=None,
        *,
        filename="image",
        mime=None,
        url="",
        limit=None,
        for_authors=False,
    ):
        """Reuse completed or concurrent identical requests across both commands.

        Args:
            image_bytes: Prepared message image bytes, if available.
            filename: Filename used when uploading the image.
            mime: Accepted for the common provider interface; encoding is verified.
            url: User-provided HTTP image URL, sent to SerpAPI without local fetch.
            limit: Candidate limit for author judgment; omitted for normal search display.
            for_authors: Prioritize X then known platforms within the bounded model pool.

        Returns:
            An independent copy of the normalized provider outcome.

        Raises:
            GoogleLensError: A credential-free error category.
        """
        del mime
        limit = (
            self.max_results
            if limit is None
            else min(MAX_CANDIDATES, max(1, int(limit)))
        )
        if self._closed:
            raise GoogleLensError("closed")
        if not self.api_key:
            raise GoogleLensError("missing_key")
        if url:
            url = source_url(url)
            if not url:
                raise GoogleLensError("image")
            identity = "url:" + url
        elif isinstance(image_bytes, bytes) and image_bytes:
            identity = "bytes:" + hashlib.sha256(image_bytes).hexdigest()
        else:
            raise GoogleLensError("image")
        key = (self.search_type, identity)
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            self._cache.move_to_end(key)
            return select_results(cached[1], limit, for_authors)
        refresh_empty = bool(cached and not cached[1].results)
        self._cache.pop(key, None)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                self._query(
                    key, image_bytes, filename, url, refresh_empty=refresh_empty
                ),
                name="soutubot.google_lens",
            )
            self._inflight[key] = task

            def completed(finished):
                if self._inflight.get(key) is finished:
                    self._inflight.pop(key, None)
                if not finished.cancelled():
                    finished.exception()

            task.add_done_callback(completed)
        return select_results(await asyncio.shield(task), limit, for_authors)

    async def _query(self, key, data, filename, url, *, refresh_empty=False):
        """Bound queueing, image preparation, upload and search by one deadline.

        Args:
            key: Cache identity for this request.
            data: Input image bytes or None.
            filename: Input filename.
            url: Input image URL or an empty string.
            refresh_empty: Bypass upstream cache after a local empty entry expires.

        Returns:
            Parsed result cached only after a successful response.
        """
        outcome = None
        try:
            async with asyncio.timeout(self.timeout):
                async with self._limit:
                    params = {
                        "engine": "google_lens",
                        "api_key": self.api_key,
                        "type": key[0],
                        "hl": "zh-cn",
                        "auto_crop": "false",
                        "no_cache": "true" if refresh_empty else "false",
                    }
                    if url:
                        params["url"] = url
                    else:
                        prepared = await prepare_image_async(
                            data, filename, max_upload_bytes=500_000
                        )
                        form = aiohttp.FormData()
                        form.add_field("api_key", self.api_key)
                        form.add_field(
                            "image",
                            prepared.data,
                            filename=prepared.filename,
                            content_type=prepared.content_type,
                        )
                        upload = await self._request("POST", "/image", data=form)
                        image_id = upload.get("image_id")
                        if (
                            not isinstance(image_id, str)
                            or not image_id
                            or len(image_id) > 4096
                        ):
                            raise GoogleLensError("response")
                        params["image_id"] = image_id
                    outcome = await self._search_once(params)
                    retry_reason = (
                        "empty"
                        if not outcome.results and self.retry_empty
                        else "no_x_source"
                        if outcome.results
                        and self.expand_sources
                        and not any(
                            source_priority(row) == 0 for row in outcome.results
                        )
                        else ""
                    )
                    if retry_reason and key[0] != "exact_matches":
                        alternate = "visual_matches" if key[0] == "all" else "all"
                        # Reuse the uploaded image and the same total deadline.
                        logger.info(
                            "Google Lens expand: reason=%s primary=%s secondary=%s",
                            retry_reason,
                            key[0],
                            alternate,
                        )
                        try:
                            secondary = await self._search_once(
                                {**params, "type": alternate, "no_cache": "true"}
                            )
                            outcome = merge_modes(outcome, secondary)
                        except (GoogleLensError, TimeoutError):
                            if not outcome.results:
                                raise
                            # A failed optional expansion must not discard known matches.
                            logger.warning(
                                "Google Lens source expansion failed; keeping primary candidates"
                            )
                            outcome.meta["expansion_failed"] = True
                    if self.cache_ttl and not outcome.meta.get("expansion_failed"):
                        ttl = (
                            self.cache_ttl
                            if outcome.results
                            else min(30, self.cache_ttl)
                        )
                        self._cache[key] = (time.monotonic() + ttl, outcome)
                        while len(self._cache) > 128:
                            self._cache.popitem(last=False)
                    return outcome
        except asyncio.TimeoutError:
            if outcome is not None and outcome.results:
                outcome.meta["expansion_failed"] = True
                logger.warning(
                    "Google Lens source expansion timed out; keeping primary candidates"
                )
                return outcome
            raise GoogleLensError("timeout") from None
        except (ImagePreparationError, ValueError):
            raise GoogleLensError("image") from None

    async def _search_once(self, params):
        """Parse one mode and log counts/IDs without URLs, titles or credentials."""
        payload = await self._request("GET", "/search.json", params=params)
        message = str(payload.get("error", "")).lower()
        empty_error = (
            "hasn't returned any results" in message
            or "has not returned any results" in message
        )
        if message and not empty_error:
            raise GoogleLensError("response")
        outcome = (
            SourceOutcome()
            if empty_error
            else parse_lens_results(payload, params["type"], MAX_CANDIDATES)
        )
        metadata = payload.get("search_metadata", {})
        search_id = metadata.get("id", "") if isinstance(metadata, dict) else ""
        search_id = (
            search_id
            if isinstance(search_id, str)
            and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", search_id)
            else "unknown"
        )
        logger.info(
            "Google Lens response: mode=%s id=%s raw=%s retained=%d invalid=%d empty_error=%s",
            params["type"],
            search_id,
            outcome.meta.get("raw_counts", {}),
            len(outcome.results),
            outcome.meta.get("invalid_rows", 0),
            empty_error,
        )
        return outcome

    async def _request(self, method, path, **kwargs):
        """Read bounded JSON only from the fixed SerpAPI origin.

        Args:
            method: GET or POST.
            path: Fixed service endpoint.
            **kwargs: Request parameters or upload form.

        Returns:
            A JSON object without echoing upstream messages in exceptions.
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                trust_env=False, timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        try:
            async with self._session.request(
                method,
                "https://serpapi.com" + path,
                proxy=self.proxy or None,
                allow_redirects=False,
                **kwargs,
            ) as response:
                if response.status in {401, 403}:
                    raise GoogleLensError("key")
                if response.status == 429:
                    raise GoogleLensError("quota")
                if response.status != 200:
                    raise GoogleLensError("response")
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > 4 * 1024 * 1024:
                        raise GoogleLensError("response")
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    raise GoogleLensError("response") from None
                if not isinstance(payload, dict):
                    raise GoogleLensError("response")
                return payload
        except aiohttp.ClientError:
            raise GoogleLensError("network") from None

    async def close(self):
        self._closed = True
        tasks = tuple(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._cache.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None


def google_lens_error(exc: Exception) -> str:
    """Describe a failure without revealing request URLs, keys or input data.

    Args:
        exc: Provider exception.

    Returns:
        A short user-facing explanation.
    """
    return {
        "missing_key": "未配置 SerpAPI Key，已跳过。",
        "key": "SerpAPI Key 无效或无权限，请检查 Google Lens 配置。",
        "quota": "SerpAPI 额度不足或请求过于频繁，请稍后再试。",
        "timeout": "查询超时。",
        "image": "图片链接无效或图片处理失败。",
    }.get(getattr(exc, "kind", ""), "查询失败，请检查网络或 Google Lens 配置。")
