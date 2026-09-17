from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin, urlparse


SOURCE_NAMES = {
    "nhentai": "NHentai",
    "ehentai": "E-Hentai",
    "panda": "Panda",
    "jmcomic": "18Comic",
    "18comic": "18Comic",
    "gelbooru": "Gelbooru",
    "danbooru": "Danbooru",
    "yande": "yande.re",
    "konachan": "Konachan",
    "anime_pictures": "Anime-Pictures",
    "zerochan": "Zerochan",
    "pixiv": "Pixiv",
    "manhuacat": "Manhuacat",
}

MIN_SIMILARITY = 80.0

LEGACY_HOSTS = {
    "nhentai": "https://nhentai.net",
    "ehentai": "https://e-hentai.org",
    "panda": "https://panda.chaika.moe",
}


class ResponseFormatError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SearchItem:
    score: float
    title: str
    source_key: str
    source_name: str
    source_id: str | None = None
    page_number: int | None = None
    thumbnail_url: str | None = None
    source_url: str | None = None
    page_url: str | None = None


@dataclass(frozen=True, slots=True)
class SearchResponse:
    result_id: str | None
    image_url: str | None
    items: tuple[SearchItem, ...]
    elapsed_seconds: float | None = None
    partial: bool = False
    base_url: str = "https://soutubot.moe"

    @property
    def result_page_url(self) -> str | None:
        if not self.result_id:
            return None
        return f"{self.base_url.rstrip('/')}/results/{quote(self.result_id, safe='')}"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def safe_http_url(value: Any, *, base_url: str | None = None) -> str | None:
    text = _text(value)
    if not text:
        return None
    if base_url and text.startswith("/"):
        text = urljoin(base_url.rstrip("/") + "/", text.lstrip("/"))
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return text


def _current_item(hit: dict[str, Any], *, base_url: str) -> SearchItem:
    segments = [_dict(value) for value in _list(hit.get("path_segments"))]
    segment = segments[0] if segments else {}
    metadata = _dict(segment.get("metadata"))
    metadata_source = _dict(metadata.get("source"))
    metadata_post = _dict(metadata.get("post"))
    links = {**_dict(segment.get("links"))}
    for key in ("thumbnail_url", "source_url", "page_url", "chapter_url"):
        if segment.get(key):
            links[key] = segment[key]

    source_key = (
        _text(
            metadata_source.get("key"),
            metadata_post.get("source_key"),
            segment.get("source_key"),
            "unknown",
        )
        or "unknown"
    ).lower()
    source_id = _text(
        metadata_source.get("id"),
        metadata_post.get("post_id"),
        segment.get("external_id"),
    )
    title_info = metadata.get("title")
    if isinstance(title_info, dict):
        title = _text(title_info.get("primary"), title_info.get("original"))
    else:
        title = _text(title_info)
    title = title or _text(segment.get("title"), hit.get("title")) or "未提供标题"
    source_name = (
        _text(metadata_source.get("name"))
        or SOURCE_NAMES.get(source_key)
        or source_key
    )

    return SearchItem(
        score=_number(hit.get("score")),
        title=title,
        source_key=source_key,
        source_name=source_name,
        source_id=source_id,
        page_number=_integer(segment.get("page_no", segment.get("page"))),
        thumbnail_url=safe_http_url(links.get("thumbnail_url"), base_url=base_url),
        source_url=safe_http_url(links.get("source_url"), base_url=base_url),
        page_url=safe_http_url(
            links.get("page_url") or links.get("chapter_url"), base_url=base_url
        ),
    )


def _legacy_item(value: dict[str, Any], *, base_url: str) -> SearchItem:
    source_key = (_text(value.get("source"), "unknown") or "unknown").lower()
    host = LEGACY_HOSTS.get(source_key)
    source_url = safe_http_url(value.get("subjectPath"), base_url=host)
    page_url = safe_http_url(value.get("pagePath"), base_url=host)
    return SearchItem(
        score=_number(value.get("similarity")),
        title=_text(value.get("title")) or "未提供标题",
        source_key=source_key,
        source_name=SOURCE_NAMES.get(source_key, source_key),
        page_number=_integer(value.get("page")),
        thumbnail_url=safe_http_url(value.get("previewImageUrl"), base_url=base_url),
        source_url=source_url,
        page_url=page_url,
    )


def parse_search_response(
    payload: Any, *, base_url: str = "https://soutubot.moe"
) -> SearchResponse:
    if not isinstance(payload, dict):
        raise ResponseFormatError("搜图服务返回的 JSON 不是对象")

    current_results = payload.get("results")
    legacy_results = payload.get("data")
    if isinstance(current_results, list):
        items = tuple(
            _current_item(_dict(hit), base_url=base_url) for hit in current_results
        )
        timing = _dict(payload.get("timing"))
        elapsed_ms = timing.get("total_ms")
        elapsed = _number(elapsed_ms) / 1000 if elapsed_ms is not None else None
        result_id = _text(payload.get("result_id"), payload.get("id"))
        image_url = safe_http_url(
            payload.get("image_url") or payload.get("imageUrl"), base_url=base_url
        )
        partial = bool(payload.get("partial") or payload.get("status") == "partial")
    elif isinstance(legacy_results, list):
        items = tuple(
            _legacy_item(_dict(hit), base_url=base_url) for hit in legacy_results
        )
        elapsed = _number(payload.get("executionTime"), default=-1.0)
        if elapsed < 0:
            elapsed = None
        result_id = _text(payload.get("id"), payload.get("result_id"))
        image_url = safe_http_url(
            payload.get("imageUrl") or payload.get("image_url"), base_url=base_url
        )
        partial = False
    else:
        raise ResponseFormatError("搜图服务响应中缺少 results/data 列表")

    return SearchResponse(
        result_id=result_id,
        image_url=image_url,
        items=items,
        elapsed_seconds=elapsed,
        partial=partial,
        base_url=base_url,
    )


def _shorten(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def select_search_items(
    response: SearchResponse,
    *,
    min_similarity: float = MIN_SIMILARITY,
    max_results: int = 3,
) -> tuple[tuple[SearchItem, ...], int]:
    qualified = sorted(
        (item for item in response.items if item.score >= min_similarity),
        key=lambda item: item.score,
        reverse=True,
    )
    return tuple(qualified[: max(1, max_results)]), len(qualified)


def format_match_header(
    qualified_count: int,
    shown_count: int,
    *,
    min_similarity: float = MIN_SIMILARITY,
) -> str:
    threshold = f"{min_similarity:g}%"
    if not qualified_count:
        return f"没有找到相似度达到 {threshold} 的结果。"
    if shown_count < qualified_count:
        return (
            f"找到 {qualified_count} 条相似度达到 {threshold} 的结果，"
            f"展示前 {shown_count} 条："
        )
    return f"找到 {qualified_count} 条相似度达到 {threshold} 的结果："


def format_search_item(item: SearchItem, index: int) -> str:
    source = item.source_name
    if item.source_id:
        source += f" #{item.source_id}"
    page = f"，第 {item.page_number} 页" if item.page_number is not None else ""
    lines = [
        f"{index}. {_shorten(item.title)}",
        f"相似度：{item.score:.2f}%｜来源：{source}{page}",
    ]
    if item.page_url:
        lines.append(f"匹配页：{item.page_url}")
    if item.source_url and item.source_url != item.page_url:
        lines.append(f"详情页：{item.source_url}")
    return "\n".join(lines)


def format_search_footer(response: SearchResponse) -> str:
    lines: list[str] = []
    if response.partial:
        lines.append("服务端返回了部分结果，可能仍有结果未展示。")
    if response.elapsed_seconds is not None:
        lines.append(f"耗时：{response.elapsed_seconds:.2f} 秒")
    lines.append("来源于搜图Bot酱")
    return "\n".join(lines)


def format_search_response(
    response: SearchResponse,
    *,
    max_results: int = 3,
    min_similarity: float = MIN_SIMILARITY,
) -> str:
    selected, qualified_count = select_search_items(
        response,
        min_similarity=min_similarity,
        max_results=max_results,
    )
    lines = [
        format_match_header(
            qualified_count,
            len(selected),
            min_similarity=min_similarity,
        )
    ]
    for index, item in enumerate(selected, start=1):
        lines.extend(["", format_search_item(item, index)])
    lines.extend(["", format_search_footer(response)])
    return "\n".join(lines).strip()
