"""Recognize publisher identities without treating a publisher as the creator."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from .google_lens_client import source_url


@dataclass(frozen=True)
class PublisherIdentity:
    platform: str = ""
    account: str = ""
    profile: str = ""
    evidence: str = ""


DOMAINS = {
    "x.com": "X (Twitter)",
    "twitter.com": "X (Twitter)",
    "pixiv.net": "Pixiv",
    "fanbox.cc": "FANBOX",
    "instagram.com": "Instagram",
    "tiktok.com": "TikTok",
    "threads.net": "Threads",
    "threads.com": "Threads",
    "facebook.com": "Facebook",
    "fb.com": "Facebook",
    "weibo.com": "微博",
    "weibo.cn": "微博",
    "bilibili.com": "哔哩哔哩",
    "b23.tv": "哔哩哔哩",
    "reddit.com": "Reddit",
    "redd.it": "Reddit",
    "pinterest.com": "Pinterest",
    "pin.it": "Pinterest",
    "tumblr.com": "Tumblr",
    "vk.com": "VK",
    "vk.ru": "VK",
    "deviantart.com": "DeviantArt",
    "artstation.com": "ArtStation",
    "flickr.com": "Flickr",
    "500px.com": "500px",
    "imgur.com": "Imgur",
    "danbooru.donmai.us": "Danbooru",
    "yande.re": "yande.re",
    "gelbooru.com": "Gelbooru",
    "konachan.com": "Konachan",
}


def publisher_display_name(url: str, title: str) -> str:
    """Read an explicit X publisher byline bound to this source URL.

    Args:
        url: The source URL containing the publisher handle.
        title: Search title returned for that exact source.

    Returns:
        A display name or an empty string, without asserting original authorship.
    """
    identity = publisher_identity(url, title)
    if not identity.account:
        return ""
    title = " ".join(title.split())
    if identity.platform == "Instagram":
        byline = re.match(
            r"^([^@()]{1,100})\s*\(@([A-Za-z0-9_.]{1,30})\)\s*(?:[•·|:]|on\b|[-–—])",
            title,
        )
        if byline and "@" + byline[2].casefold() == identity.account.casefold():
            return byline[1].strip()
        return ""
    if identity.platform == "DeviantArt":
        byline = re.search(
            r"\bby\s+([A-Za-z0-9_-]+)\s+on\s+DeviantArt\s*$", title, re.I
        )
        return (
            byline[1]
            if byline and "@" + byline[1].casefold() == identity.account.casefold()
            else ""
        )
    if identity.platform != "X (Twitter)":
        return ""
    byline = re.match(r"^(.{1,100}?)\s+on\s+(?:X|Twitter)\s*[:：]", title, re.I)
    profile = re.fullmatch(
        r"(.{1,100}?)\s*\(@([A-Za-z0-9_]{1,15})\)\s*(?:/|[|｜])\s*(?:X|Twitter)",
        title,
        re.I,
    )
    name = byline[1].strip() if byline else ""
    if profile:
        if "@" + profile[2].casefold() != identity.account.casefold():
            return ""
        name = profile[1].strip()
    handles = re.findall(r"@([A-Za-z0-9_]+)", name)
    if handles:
        if (
            len(handles) != 1
            or "@" + handles[0].casefold() != identity.account.casefold()
        ):
            return ""
        name = re.sub(r"\s*\(@[A-Za-z0-9_]+\)\s*$", "", name).strip()
    if not name or "http" in name.lower() or any(ord(c) < 32 for c in name):
        return ""
    return name


def publisher_identity(url: str, title: str = "") -> PublisherIdentity:
    """Parse only explicit profile owners or platform-bound title bylines.

    Args:
        url: A returned source/profile URL, never fetched by this function.
        title: Search title used only for explicit publisher bylines.

    Returns:
        Platform and grounded publisher identity; missing fields stay empty.
        This does not assert that the publisher created the pictured work.
    """
    if not source_url(url):
        return PublisherIdentity()
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    domain = next((d for d in DOMAINS if host == d or host.endswith("." + d)), "")
    platform = DOMAINS.get(domain, "")
    if not platform:
        return PublisherIdentity()
    path = parsed.path.rstrip("/") or "/"
    account = profile = evidence = ""
    match = None
    # Full path matching prevents post IDs, reserved routes and subreddit names
    # from being mistaken for usernames. Short links remain unresolved evidence.
    if domain in {"x.com", "twitter.com"} and host in {
        domain,
        "www." + domain,
        "mobile." + domain,
    }:
        match = re.fullmatch(
            r"/([A-Za-z0-9_]{1,15})(?:/status/\d+(?:/photo/\d+)?)?", path
        )
        if match and match[1].lower() not in {
            "i",
            "home",
            "search",
            "intent",
            "explore",
            "share",
            "hashtag",
            "settings",
            "messages",
            "notifications",
        }:
            account, profile = match[1], "https://x.com/" + match[1]
    elif domain == "pixiv.net" and host in {domain, "www." + domain}:
        match = re.fullmatch(r"/(?:[a-z]{2}/)?users/(\d+)", path)
        fanbox = re.fullmatch(r"/fanbox/creator/(\d+)(?:/post/\d+)?", path)
        if match:
            account, profile = match[1], "https://www.pixiv.net/users/" + match[1]
        elif fanbox:
            platform, account, profile = (
                "FANBOX",
                fanbox[1],
                "https://www.pixiv.net/fanbox/creator/" + fanbox[1],
            )
    elif domain in {"tiktok.com", "threads.net", "threads.com"} and host in {
        domain,
        "www." + domain,
    }:
        tail = (
            r"(?:/video/\d+)?"
            if domain == "tiktok.com"
            else r"(?:/post/[A-Za-z0-9_-]+)?"
        )
        match = re.fullmatch(r"/@([A-Za-z0-9_.]{1,40})" + tail, path)
        if match:
            account, profile = match[1], "https://www." + domain + "/@" + match[1]
    elif domain == "instagram.com" and host in {domain, "www." + domain}:
        match = re.fullmatch(r"/([A-Za-z0-9_.]{1,30})", path)
        if match and match[1].lower() not in {
            "p",
            "reel",
            "reels",
            "stories",
            "explore",
            "accounts",
            "direct",
            "about",
            "developer",
            "legal",
            "web",
        }:
            account, profile = match[1], "https://www.instagram.com/" + match[1] + "/"
    elif domain in {"facebook.com", "fb.com"} and host in {
        domain,
        "www." + domain,
        "m." + domain,
    }:
        match = re.fullmatch(r"/([A-Za-z0-9.]+)(?:/posts/[A-Za-z0-9]+)?", path)
        if path == "/profile.php":
            ids = parse_qs(parsed.query).get("id", [])
            if len(ids) == 1 and ids[0].isdigit():
                account, profile = (
                    ids[0],
                    "https://www.facebook.com/profile.php?id=" + ids[0],
                )
        elif match and match[1].lower() not in {
            "watch",
            "photo.php",
            "permalink.php",
            "story.php",
            "reel",
            "groups",
            "share",
            "login",
            "help",
            "pages",
            "events",
            "marketplace",
            "gaming",
        }:
            account, profile = match[1], "https://www.facebook.com/" + match[1]
    elif domain in {"weibo.com", "weibo.cn"} and host in {
        domain,
        "www." + domain,
        "m." + domain,
    }:
        match = re.fullmatch(r"/(?:u/)?(\d{1,20})(?:/[A-Za-z0-9]+)?", path)
        if match:
            account, profile = match[1], "https://weibo.com/u/" + match[1]
    elif host == "space.bilibili.com":
        match = re.fullmatch(r"/(\d{1,20})(?:/.*)?", path)
        if match:
            account, profile = match[1], "https://space.bilibili.com/" + match[1]
    elif domain == "reddit.com" and host in {
        domain,
        "www." + domain,
        "old." + domain,
        "new." + domain,
    }:
        match = re.fullmatch(
            r"/(?:u|user)/([A-Za-z0-9_-]{1,40})(?:/comments/[A-Za-z0-9]+(?:/[^/]+)?)?",
            path,
        )
        if match:
            account, profile = match[1], "https://www.reddit.com/user/" + match[1] + "/"
    elif domain == "pinterest.com" and host in {domain, "www." + domain}:
        match = re.fullmatch(r"/([A-Za-z0-9_]{1,40})", path)
        if match and match[1].lower() not in {
            "pin",
            "search",
            "ideas",
            "business",
            "login",
            "settings",
            "today",
            "explore",
        }:
            account, profile = match[1], "https://www.pinterest.com/" + match[1] + "/"
    elif domain == "tumblr.com":
        if host in {domain, "www." + domain}:
            match = re.fullmatch(r"/([A-Za-z0-9-]{1,32})(?:/\d+(?:/[^/]+)?)?", path)
            handle = match[1] if match else ""
        else:
            handle = host.removesuffix(".tumblr.com")
        if re.fullmatch(
            r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", handle
        ) and handle.lower() not in {
            "www",
            "assets",
            "static",
            "media",
            "dashboard",
            "explore",
            "search",
            "tagged",
            "blog",
            "settings",
            "login",
        }:
            account, profile = handle, "https://www.tumblr.com/" + handle
    elif domain in {"vk.com", "vk.ru"} and host in {
        domain,
        "www." + domain,
        "m." + domain,
    }:
        wall = re.fullmatch(r"/wall(-?\d+)_\d+", path)
        match = re.fullmatch(r"/([A-Za-z0-9_.]{1,50})", path)
        if wall:
            account = (
                ("club" + wall[1][1:]) if wall[1].startswith("-") else "id" + wall[1]
            )
        elif (
            match
            and match[1].lower()
            not in {
                "feed",
                "search",
                "login",
                "video",
                "photo",
                "albums",
                "wall",
                "im",
                "groups",
            }
            and not re.match(r"(?:photo|video|album|wall)-?\d", match[1])
        ):
            account = match[1]
        if account:
            profile = "https://vk.com/" + account
    elif domain == "deviantart.com" and host in {domain, "www." + domain}:
        match = re.fullmatch(r"/([A-Za-z0-9_-]{1,50})(?:/art/[^/]+)?", path)
        if match and match[1].lower() not in {
            "tag",
            "search",
            "join",
            "login",
            "settings",
            "shop",
        }:
            account, profile = match[1], "https://www.deviantart.com/" + match[1]
    elif domain in {"artstation.com", "fanbox.cc"}:
        if host not in {domain, "www." + domain}:
            handle = host.removesuffix("." + domain)
        else:
            match = (
                re.fullmatch(r"/@([A-Za-z0-9_-]+)", path)
                if domain == "fanbox.cc"
                else re.fullmatch(r"/([A-Za-z0-9_-]+)", path)
            )
            handle = match[1] if match else ""
        if re.fullmatch(r"[A-Za-z0-9_-]{1,60}", handle) and handle.lower() not in {
            "www",
            "assets",
            "cdn",
            "support",
            "help",
            "about",
            "artwork",
            "search",
            "marketplace",
            "learning",
            "jobs",
            "prints",
            "blogs",
            "channels",
        }:
            account = handle
            profile = (
                "https://" + handle + "." + domain
                if domain == "fanbox.cc"
                else "https://www.artstation.com/" + handle
            )
    elif domain == "flickr.com" and host in {domain, "www." + domain}:
        match = re.fullmatch(r"/(?:photos|people)/([A-Za-z0-9_@-]+)(?:/\d+)?", path)
        if match:
            account, profile = (
                match[1],
                "https://www.flickr.com/people/" + match[1] + "/",
            )
    elif domain == "500px.com" and host in {domain, "www." + domain}:
        match = re.fullmatch(r"/(?:p/|@)([A-Za-z0-9_-]+)", path)
        if match:
            account, profile = match[1], "https://500px.com/p/" + match[1]
    elif domain == "imgur.com" and host in {domain, "www." + domain}:
        match = re.fullmatch(r"/user/([A-Za-z0-9_-]+)(?:/posts)?", path)
        if match:
            account, profile = match[1], "https://imgur.com/user/" + match[1]
    if account:
        evidence = url
    # A title byline can identify a publisher when the post URL contains only an
    # opaque ID. Free-floating credits/mentions are not publisher identities.
    if (
        not account
        and platform == "Instagram"
        and host in {"instagram.com", "www.instagram.com"}
        and re.fullmatch(r"/(?:p|reel)/[A-Za-z0-9_-]+", path)
    ):
        byline = re.match(
            r"^[^@()\n]{1,100}\s*\(@([A-Za-z0-9_.]{1,30})\)\s*(?:[•·|:]|on\b|[-–—])",
            title,
        )
        if byline and not re.search(
            r"\b(?:repost|credit|via|from)\b|转载|搬运|原作者", byline[0], re.I
        ):
            account, evidence = byline[1], byline[0]
            profile = (
                ("https://www.instagram.com/" + account + "/")
                if platform == "Instagram"
                else "https://www." + domain + "/@" + account
            )
    return PublisherIdentity(
        platform, "@" + account if account else "", profile, evidence
    )
