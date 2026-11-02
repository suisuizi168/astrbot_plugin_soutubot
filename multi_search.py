"""Optional parallel providers. Failure and cache are isolated per provider."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import time
import logging
from collections import OrderedDict
from .google_lens_client import GoogleLensClient

logger = logging.getLogger("astrbot")


class MultiSearchService:
    def __init__(self, config, *, google_lens=None):
        self.config = config
        self.enabled = bool(config.get("multi_search_enabled", True))
        self._clients = {}
        self._cache = OrderedDict()
        self._limit = asyncio.Semaphore(2)
        self._owns_google_lens = google_lens is None
        self._google_lens = (
            google_lens if google_lens is not None else GoogleLensClient(config)
        )

    async def search(self, image):
        if not self.enabled:
            return ""
        names = [
            name
            for name in ("saucenao", "yandex", "ascii2d")
            if self.config.get(name + "_enabled", name != "ascii2d")
        ]
        if self._google_lens.search_enabled:
            names.append("google_lens")
        if not names:
            return "多引擎搜图已开启，但没有启用任何搜索源。"
        async with self._limit:
            sections = await asyncio.gather(
                *(self._section(name, image) for name in names)
            )
        return "多引擎搜图结果：\n\n" + "\n\n".join(sections)

    async def _section(self, name, image):
        if name == "google_lens":
            try:
                outcome = await self._google_lens.search(
                    image.data, filename=image.filename, mime=image.content_type
                )
                formatter = importlib.import_module(".engines.formatter", __package__)
                blocks = formatter.format_outcome(
                    formatter.SourceOutcome(results=outcome.results),
                    nsfw_send_image=False,
                    max_results=self._google_lens.max_results,
                    header="【图片来源】",
                    max_chars=2000,
                )
                return "\n".join(
                    block["text"] for block in blocks if block.get("type") == "text"
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Google Lens search failed: %s", getattr(exc, "kind", type(exc).__name__))
                return "【图片来源】查询出现错误，请稍后重试。"
        label = {"saucenao": "SauceNAO", "yandex": "Yandex", "ascii2d": "ascii2d"}[name]
        if (
            name == "saucenao"
            and not str(self.config.get("saucenao_api_key", "")).strip()
        ):
            return f"【{label}】未配置 API Key，已跳过。"
        try:
            timeout = max(
                5, min(120, int(self.config.get("multi_search_timeout_seconds", 30)))
            )
            key = (name, hashlib.sha256(image.data).hexdigest())
            cached = self._cache.get(key)
            if cached and cached[0] > time.monotonic():
                outcome = cached[1]
                self._cache.move_to_end(key)
            else:
                if name not in self._clients:
                    module = importlib.import_module(
                        f".engines.{name}_client", __package__
                    )
                    if name == "saucenao":
                        client = module.SaucenaoClient(
                            api_key=str(self.config.get("saucenao_api_key", "")),
                            timeout=timeout,
                            min_similarity=int(
                                self.config.get("saucenao_min_similarity", 50)
                            ),
                        )
                    elif name == "yandex":
                        client = module.YandexClient(
                            timeout=timeout, top_k=9, max_per_domain=2
                        )
                    else:
                        client = module.Ascii2dClient(
                            timeout=timeout,
                            bovw=bool(self.config.get("ascii2d_bovw", False)),
                        )
                    self._clients[name] = client
                outcome = await asyncio.wait_for(
                    self._clients[name].search(
                        image.data, filename=image.filename, mime=image.content_type
                    ),
                    timeout,
                )
                ttl = max(0, int(self.config.get("multi_search_cache_ttl", 3600)))
                if ttl:
                    self._cache[key] = (time.monotonic() + ttl, outcome)
                    while len(self._cache) > 128:
                        self._cache.popitem(last=False)
            formatter = importlib.import_module(".engines.formatter", __package__)
            blocks = formatter.format_outcome(
                outcome,
                nsfw_send_image=False,
                max_results=max(1, min(10, int(self.config.get("max_results", 3)))),
                header=f"【{label}】",
                max_chars=1600,
            )
            return "\n".join(
                block["text"] for block in blocks if block.get("type") == "text"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Provider errors may include credential-bearing URLs. Don't echo them.
            return f"【{label}】查询失败（{type(exc).__name__}），其余来源不受影响。"

    async def close(self):
        await asyncio.gather(
            *(c.close() for c in self._clients.values()), return_exceptions=True
        )
        self._clients.clear()
        self._cache.clear()
        if self._owns_google_lens:
            await self._google_lens.close()
