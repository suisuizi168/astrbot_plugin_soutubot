"""Bounded, best-effort JM album-ID lookup for displayed Soutu results."""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Literal

try:
    from astrbot.api import logger
except ImportError:
    logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class JmCandidate:
    album_id: str
    title: str
    score: float


@dataclass(frozen=True, slots=True)
class JmResolution:
    query_title: str
    status: Literal["matched", "candidates", "not_found", "failed", "skipped"]
    selected: JmCandidate | None = None
    candidates: tuple[JmCandidate, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class JmResolverSettings:
    enabled: bool = True
    timeout_seconds: int = 6
    retry_times: int = 0
    domain_list: tuple[str, ...] = ()
    proxy: str = "system"
    auto_select_threshold: float = 90.0
    min_candidate_threshold: float = 55.0
    auto_select_margin: float = 8.0
    max_raw_candidates: int = 20
    max_display_candidates: int = 3
    author_max_pages: int = 3
    max_author_candidates: int = 60


_ZERO_WIDTH = re.compile(r"[\u200b-\u200d\ufeff]")
_SPACE_OR_SEPARATOR = re.compile(r"[\s_/|\\\\\-—–]+")
_METADATA = re.compile(
    r"(?:中文|汉化|翻译|修正|扫描组|dl版|合集|digital|uncensored|"
    r"chinese|english|[cＣ]\d+|comic\d+)",
    re.IGNORECASE,
)
_EDGE_BLOCK = re.compile(r"^(?:\[([^\[\]]*)\]|\(([^()]*)\)|（([^（）]*)）)")
_TRAILING_BLOCK = re.compile(r"(?:\[([^\[\]]*)\]|\(([^()]*)\)|（([^（）]*)）)$")
_NON_WORD = re.compile(r"[^\w\u4e00-\u9fff\u3040-\u30ff]+", re.UNICODE)


def _metadata_block(match: re.Match[str]) -> bool:
    return bool(_METADATA.search(next(value for value in match.groups() if value is not None)))


def normalize_jm_title(title: str) -> str:
    """Normalize safely without discarding parentheses that may identify a work."""
    value = unicodedata.normalize("NFKC", str(title or "")).casefold()
    value = _ZERO_WIDTH.sub("", value)
    value = _SPACE_OR_SEPARATOR.sub(" ", value).strip()
    while value:
        leading = _EDGE_BLOCK.match(value)
        if leading and _metadata_block(leading):
            value = value[leading.end() :].strip()
            continue
        trailing = _TRAILING_BLOCK.search(value)
        if trailing and _metadata_block(trailing):
            value = value[: trailing.start()].strip()
            continue
        break
    return _NON_WORD.sub("", value)


def _bigram_dice(left: str, right: str) -> float:
    if len(left) < 2 or len(right) < 2:
        return 0.0
    left_pairs = {left[index : index + 2] for index in range(len(left) - 1)}
    right_pairs = {right[index : index + 2] for index in range(len(right) - 1)}
    if not left_pairs or not right_pairs:
        return 0.0
    return 2 * len(left_pairs & right_pairs) / (len(left_pairs) + len(right_pairs))


def score_jm_title(query: str, candidate: str) -> float:
    left = normalize_jm_title(query)
    right = normalize_jm_title(candidate)
    if not left or not right:
        return 0.0
    if left == right:
        return 100.0
    if min(len(left), len(right)) >= 8 and (left in right or right in left):
        return 95.0
    sequence = difflib.SequenceMatcher(a=left, b=right, autojunk=False).ratio()
    return round(min(100.0, max(0.0, (0.6 * sequence + 0.4 * _bigram_dice(left, right)) * 100)), 1)


def format_jm_resolution(resolution: JmResolution | None) -> str | None:
    if resolution is None or resolution.status == "skipped":
        return None
    if resolution.status == "matched" and resolution.selected is not None:
        candidate = resolution.selected
        if resolution.reason == "title_match":
            source = "标题匹配"
        elif resolution.reason == "author_match":
            source = "作者匹配"
        else:
            source = "已确认"
        return f"JM {source}：JM{candidate.album_id}（{candidate.title}｜匹配 {candidate.score:.1f}%）"
    if resolution.status == "candidates":
        lines = ["JM 候选（请核对）："]
        lines.extend(
            f"{index}. JM{item.album_id}｜{item.title}｜匹配 {item.score:.1f}%"
            for index, item in enumerate(resolution.candidates, start=1)
        )
        return "\n".join(lines)
    if resolution.status == "not_found":
        if resolution.reason == "title_not_found":
            return "未通过 JM 标题检索到对应作品。"
        if resolution.reason == "title_no_author":
            return "未通过 JM 标题检索到对应作品，且未提取到可用于作者匹配的信息。"
        if resolution.reason == "title_disabled":
            return "JM：标题检索已关闭，且没有可用作者信息继续匹配。"
        if resolution.reason == "title_author_disabled":
            return "未通过 JM 标题检索到对应作品；作者作品匹配已关闭。"
        if resolution.reason == "author_not_found":
            return "未能在该作者的作品中匹配到对应作品。"
        if resolution.reason == "author_unconfirmed":
            return "未匹配到符合搜图标题的作品。"
        return "JM：未找到可确认的匹配作品。"
    if resolution.reason == "llm_timeout":
        return "JM：标题检索词分析超时，已保留原搜图结果。"
    if resolution.reason == "title_timeout":
        return "JM 标题检索超时，未能确认对应作品。"
    if resolution.reason == "title_error":
        return "JM 标题检索失败，未能确认对应作品。"
    if resolution.reason == "author_timeout":
        return "JM 作者作品匹配超时，未能确认对应作品。"
    if resolution.reason == "author_error":
        return "JM 作者作品匹配失败，未能确认对应作品。"
    if resolution.reason == "author_verify_timeout":
        return "JM：已取得作者候选，但作品核验超时，无法确认匹配。"
    if resolution.reason == "author_verify_error":
        return "JM：已取得作者候选，但作品核验失败，无法确认匹配。"
    if resolution.reason in {"llm_error", "internal_error"}:
        return "JM：检索过程中发生错误，已保留原搜图结果。"
    return "JM 查询暂不可用，已保留原搜图结果。"


class JmResolver:
    def __init__(
        self,
        settings: JmResolverSettings,
        *,
        client_factory: Callable[[JmResolverSettings], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory or self._default_client_factory
        self._executor: ThreadPoolExecutor | None = None

    @staticmethod
    def _default_client_factory(settings: JmResolverSettings) -> Any:
        # Delay the optional dependency import so a missing package cannot break /找本.
        from jmcomic import JmOption

        client_settings: dict[str, Any] = {
            "impl": "api",
            "retry_times": settings.retry_times,
            "timeout": settings.timeout_seconds,
            "domain": {"api": list(settings.domain_list)},
            "postman": {"meta_data": {"proxies": settings.proxy}},
        }
        option = JmOption.construct({"log": False, "client": client_settings})
        return option.new_jm_client(impl="api")

    @staticmethod
    def _to_candidates(
        raw: list[tuple[Any, Any]], *, score_query: str | None
    ) -> tuple[JmCandidate, ...]:
        """Deduplicate official JM records while retaining them for later LLM verification."""
        unique: dict[tuple[str, str], JmCandidate] = {}
        for album_id, title in raw:
            album_text = str(album_id).strip()
            title_text = str(title).strip()
            normalized = normalize_jm_title(title_text)
            if not album_text.isdigit() or not normalized:
                continue
            score = score_jm_title(score_query, title_text) if score_query else 0.0
            candidate = JmCandidate(album_text, title_text, score)
            key = (album_text, normalized)
            if key not in unique or candidate.score > unique[key].score:
                unique[key] = candidate
        return tuple(sorted(unique.values(), key=lambda item: (-item.score, item.album_id)))

    def _search(self, source_title: str) -> tuple[JmCandidate, ...]:
        started = time.monotonic()
        raw: list[tuple[Any, Any]] = []
        client = self._client_factory(self._settings)
        page = client.search_site(source_title, page=1)
        iterator = page.iter_id_title()
        for entry in iterator:
            if len(raw) >= self._settings.max_raw_candidates:
                break
            try:
                album_id, title = entry
            except (TypeError, ValueError):
                continue
            raw.append((album_id, title))
        candidates = self._to_candidates(raw, score_query=source_title)
        return candidates

    def _get_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=5,
                thread_name_prefix="soutubot-jm",
            )
        return self._executor

    async def _run_in_worker(self, func: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._get_executor(), func, *args)

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def _search_author(self, author: str) -> tuple[JmCandidate, ...]:
        """Collect bounded pages of an author search; title matching happens in the caller."""
        raw: list[tuple[Any, Any]] = []
        client = self._client_factory(self._settings)
        for page_number in range(1, self._settings.author_max_pages + 1):
            page = client.search_author(author, page=page_number)
            valid_entries = 0
            for entry in page.iter_id_title():
                if len(raw) >= self._settings.max_author_candidates:
                    break
                try:
                    album_id, title = entry
                except (TypeError, ValueError):
                    continue
                raw.append((album_id, title))
                valid_entries += 1
            if len(raw) >= self._settings.max_author_candidates or not valid_entries:
                break
            try:
                if page_number >= int(getattr(page, "page_count", page_number)):
                    break
            except (TypeError, ValueError):
                break
        return self._to_candidates(raw, score_query=None)

    async def resolve(
        self, source_title: str, *, total_timeout_seconds: float | None = None
    ) -> JmResolution:
        if not self._settings.enabled:
            return JmResolution(source_title, "skipped", reason="disabled")
        if len(normalize_jm_title(source_title)) < 3:
            return JmResolution(source_title, "skipped", reason="invalid_title")
        timeout = (
            total_timeout_seconds
            if total_timeout_seconds is not None
            else self._settings.timeout_seconds * 1.1
        )
        try:
            candidates = await asyncio.wait_for(
                self._run_in_worker(self._search, source_title),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return JmResolution(source_title, "failed", reason="timeout")
        except ImportError:
            return JmResolution(source_title, "failed", reason="dependency_error")
        except Exception:
            return JmResolution(source_title, "failed", reason="request_error")
        eligible = tuple(
            item for item in candidates if item.score >= self._settings.min_candidate_threshold
        )
        if not eligible:
            return JmResolution(source_title, "not_found")
        first = eligible[0]
        second = eligible[1] if len(eligible) > 1 else None
        margin = first.score - second.score if second is not None else float("inf")
        if first.score >= self._settings.auto_select_threshold and margin >= self._settings.auto_select_margin:
            return JmResolution(source_title, "matched", selected=first)
        return JmResolution(
            source_title,
            "candidates",
            candidates=eligible[: self._settings.max_display_candidates],
        )

    async def search_author(
        self, author: str, *, total_timeout_seconds: float | None = None
    ) -> JmResolution:
        """Return bounded official JM titles for an author without title-score filtering."""
        if not self._settings.enabled:
            return JmResolution(author, "skipped", reason="disabled")
        if len(normalize_jm_title(author)) < 2:
            return JmResolution(author, "skipped", reason="invalid_author")
        timeout = (
            total_timeout_seconds
            if total_timeout_seconds is not None
            else self._settings.timeout_seconds * 1.1
        )
        try:
            candidates = await asyncio.wait_for(
                self._run_in_worker(self._search_author, author),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return JmResolution(author, "failed", reason="timeout")
        except ImportError:
            return JmResolution(author, "failed", reason="dependency_error")
        except Exception:
            return JmResolution(author, "failed", reason="request_error")
        if not candidates:
            return JmResolution(author, "not_found")
        return JmResolution(author, "candidates", candidates=candidates)
