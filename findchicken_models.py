"""Parse SauceNAO author metadata and render the find-chicken text contract."""

from __future__ import annotations

from .user_messages import FINDCHICKEN_EMPTY

import math
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .google_lens_client import source_key, source_url


PLATFORMS = {
    0: "H-Magazines",
    1: "H-Anime",
    2: "H-Game CG",
    3: "Doujinshi DB",
    4: "Doujinshi DB Samples",
    5: "Pixiv",
    6: "Pixiv",
    7: "Anime",
    8: "Nico Nico Seiga",
    9: "Danbooru",
    10: "drawr",
    11: "Nijie",
    12: "yande.re",
    13: "Anime Openings",
    14: "IMDb",
    15: "Shutterstock",
    16: "FAKKU",
    18: "H-Misc (nhentai)",
    19: "2D Market",
    20: "MediBang",
    21: "Anime",
    22: "H-Anime",
    23: "Movies",
    24: "Shows",
    25: "Gelbooru",
    26: "Konachan",
    27: "Sankaku Channel",
    28: "Anime-Pictures",
    29: "e621",
    30: "Idol Complex",
    31: "bcy illust",
    32: "bcy cosplay",
    33: "PortalGraphics",
    34: "deviantArt",
    35: "Pawoo",
    36: "Madokami",
    37: "MangaDex",
    38: "H-Misc (ehentai)",
    39: "ArtStation",
    40: "FurAffinity",
    41: "X (Twitter)",
    42: "Furry Network",
    43: "Kemono",
    44: "Skeb",
}


@dataclass(frozen=True)
class AuthorMatch:
    platform: str
    name: str
    account: str
    similarity: float | None
    url: str = ""
    source_url: str = ""
    title: str = ""
    fallback: bool = False


def _author_url(index: int, data: dict) -> str:
    """Prefer a known author profile, otherwise use a returned source URL.

    Args:
        index: SauceNAO source index.
        data: Source metadata returned for this result.

    Returns:
        An HTTP(S) profile or source address, or an empty string.
    """
    if index == 41:
        handle = _text(data.get("twitter_user_handle")).removeprefix("@")
        if re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
            return f"https://x.com/{handle}"
    if index in (5, 6):
        member_id = _text(data.get("member_id"))
        if re.fullmatch(r"[0-9]+", member_id):
            return f"https://www.pixiv.net/users/{member_id}"
    urls = data.get("ext_urls", [])
    if isinstance(urls, str):
        urls = [urls]
    candidates = list(urls) if isinstance(urls, list) else []
    candidates.append(data.get("source"))
    for value in candidates:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if len(value) > 2048 or any(char.isspace() or ord(char) < 32 for char in value):
            continue
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and not parsed.username
                and not parsed.password
            ):
                return value
        except ValueError:
            pass
    return ""


def _text(value) -> str:
    if isinstance(value, list):
        return ", ".join(filter(None, (_text(item) for item in value)))[:200]
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return " ".join(str(value).split())[:200]


def author_matches(payload, threshold: float, limit: int) -> list[AuthorMatch]:
    """Keep API order and reject results below the configured threshold.

    Args:
        payload: SauceNAO JSON response.
        threshold: Minimum similarity in percent.
        limit: Maximum number of results.

    Returns:
        Author identities available in the response, without extra lookups.
    """
    rows = payload.get("results", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return []
    matches = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        header, data = row.get("header"), row.get("data")
        header = header if isinstance(header, dict) else {}
        data = data if isinstance(data, dict) else {}
        try:
            score = float(header.get("similarity", 0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(score) or not 0 <= score <= 100 or score < threshold:
            continue
        try:
            index = int(header.get("index_id", -1))
        except (TypeError, ValueError):
            index = -1
        platform = PLATFORMS.get(index)
        if not platform:
            platform = re.sub(r"^Index #\d+:\s*", "", _text(header.get("index_name")))
            platform = platform.split(" - ", 1)[0] or "未知平台"
        author = next(
            (
                value
                for key in (
                    "author",
                    "member_name",
                    "author_name",
                    "twitter_user_handle",
                    "creator",
                    "user_name",
                    "artist",
                    "company",
                )
                if (value := _text(data.get(key)))
            ),
            "",
        )
        title = next(
            (
                value
                for key in ("title", "eng_name", "jp_name", "material", "source")
                if (value := _text(data.get(key)))
            ),
            "",
        )
        account = _text(data.get("member_id")) if index in (5, 6) else ""
        account = (
            account
            or _text(data.get("twitter_user_handle"))
            or _text(data.get("user_name"))
        )
        account = (
            (account if account.startswith("@") else "@" + account)
            if account
            else "未知"
        )
        urls = data.get("ext_urls", [])
        urls = [urls] if isinstance(urls, str) else urls
        urls = list(urls) if isinstance(urls, list) else []
        urls.append(data.get("source"))
        original_url = next((url for value in urls if (url := source_url(value))), "")
        matches.append(
            AuthorMatch(
                platform,
                author or "未知",
                account,
                score,
                _author_url(index, data),
                original_url,
                title,
            )
        )
        if len(matches) >= limit:
            break
    return matches


def format_authors(
    matches: list[AuthorMatch],
    *,
    direct_url: bool,
    show_similarity: bool,
    google_results=(),
    notices=(),
) -> str:
    """Render author metadata with emoji and an optional clickable URL.

    Args:
        matches: Filtered author matches.
        direct_url: Whether the user supplied an image link as text.
        show_similarity: Whether to show the score for visual matches.
        google_results: Optional normalized web matches, without assumed authors.
        notices: Bounded provider failures or skip notices.

    Returns:
        One text message with the agreed header and footer.
    """
    del direct_url, google_results, notices
    if not matches:
        return FINDCHICKEN_EMPTY
    match = matches[0]
    label = "视觉匹配"
    if show_similarity and match.similarity is not None:
        label += f"（相似度 {match.similarity:.1f}%）"
    heading = "🎯 找到匹配来源" if match.fallback else "🎯 已找到鸡"
    text = f"{heading}\n🌐 平台：{match.platform}\n匹配：{label}\n👤 名字：{match.name}\n🆔 账号：{match.account}"
    if match.url:
        text += f"\n{match.url}"
    if match.source_url and source_key(match.source_url) != source_key(match.url):
        text += f"\n🔗 出处：{match.source_url}"
    footer = (
        "⚠未确认原作者，以上为匹配度优先的来源。"
        if match.fallback
        else "⚠识图存在误差，请自行核对哦~"
    )
    return text + "\n\n" + footer
