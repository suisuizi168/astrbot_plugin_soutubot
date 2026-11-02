"""Pure source selection rules. No HTTP, LLM, download or messaging side effects."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from urllib.parse import urlparse

@dataclass(frozen=True,slots=True)
class DownloadRoute:
    action: str  # nh / jm / query_jm / none
    album_id: str = ''

def source_id(item, source):
    keys={'nh':{'nhentai'},'jm':{'jmcomic','18comic'}}[source]
    if item.source_key not in keys: return ''
    value=str(item.source_id or '')
    if re.fullmatch(r'[0-9]{1,12}',value): return value
    for value in (item.source_url,item.page_url):
        if not value: continue
        url=urlparse(value)
        if source=='nh' and url.hostname not in {'nhentai.net','www.nhentai.net'}: continue
        pattern=r'/g/([0-9]{1,12})(?:/|$)' if source=='nh' else r'/(?:album|photo)/([0-9]{1,12})(?:/|$)'
        match=re.search(pattern,url.path)
        if match: return match.group(1)
    return ''

def target_response(response, threshold):
    qualified=sorted((i for i in response.items if i.score>=threshold),key=lambda i:i.score,reverse=True)
    if not qualified: return replace(response,items=())
    first=qualified[0]
    # A result's source segments belong to one hit; don't pick an unrelated
    # lower-scoring NH work merely because NH has download priority.
    items=tuple(i for i in qualified if first.hit_index>=0 and i.hit_index==first.hit_index) if first.hit_index>=0 else (first,)
    return replace(response,items=items)

def choose_download_route(response, *, nh_enabled, jm_enabled):
    if nh_enabled:
        for item in response.items:
            identifier=source_id(item,'nh')
            if identifier: return DownloadRoute('nh',identifier)
    if jm_enabled:
        for item in response.items:
            identifier=source_id(item,'jm')
            if identifier: return DownloadRoute('jm',identifier)
        if response.items: return DownloadRoute('query_jm')
    return DownloadRoute('none')
