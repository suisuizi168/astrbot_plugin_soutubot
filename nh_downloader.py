"""Independent NH API/image adapter. No LLM, PDF or message handling here."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

if __package__:
    from .download_service import DownloadService, DownloadSettings, ImageBundle, DownloadError, AlbumNotFoundError, PartialDownloadError
else:
    from download_service import DownloadService, DownloadSettings, ImageBundle, DownloadError, AlbumNotFoundError, PartialDownloadError

def normalize_nh_id(value):
    text=str(value or '').strip()
    match=re.fullmatch(r'(?i)(?:NH\s*)?([0-9]{1,12})',text)
    if match: return match.group(1)
    url=urlparse(text)
    if url.scheme=='https' and url.netloc.lower() in {'nhentai.net','www.nhentai.net'} and not url.query and not url.fragment:
        match=re.fullmatch(r'/g/([0-9]{1,12})/?',url.path)
        if match: return match.group(1)
    raise ValueError('请提供 NH 编号或 https://nhentai.net/g/编号/ 链接')

@dataclass(frozen=True,slots=True)
class NhDownloadSettings(DownloadSettings):
    request_timeout: int = 30
    image_concurrency: int = 4
    max_pages: int = 2000
    max_image_bytes: int = 30*1024*1024
    max_total_bytes: int = 1024*1024*1024

def parse_gallery(payload):
    if not isinstance(payload,dict): raise DownloadError('NH 响应格式异常')
    title=payload.get('title') or {}
    name=next((str(title[k]) for k in ('pretty','japanese','english') if isinstance(title,dict) and title.get(k)), 'NH')
    pages=payload.get('pages')
    if isinstance(pages,list):
        paths=[]
        for page in pages:
            raw=page.get('path') if isinstance(page,dict) else None
            if not isinstance(raw,str) or not re.fullmatch(r'/?(?:[A-Za-z0-9_-]+/)+[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|gif|webp)',raw):
                raise DownloadError('NH 图片路径异常')
            paths.append('/'+raw.lstrip('/'))
    else:
        media=str(payload.get('media_id',''))
        images=payload.get('images') or {}
        pages=images.get('pages') if isinstance(images,dict) else None
        if not media.isascii() or not media.isdecimal() or not isinstance(pages,list):
            raise DownloadError('NH 缺少图片清单')
        extensions={'j':'jpg','p':'png','g':'gif','w':'webp'}
        paths=[]
        for n,page in enumerate(pages,1):
            ext=extensions.get(page.get('t')) if isinstance(page,dict) else None
            if not ext: raise DownloadError('NH 图片类型暂不支持')
            paths.append(f'/galleries/{media}/{n}.{ext}')
    if not paths: raise AlbumNotFoundError('NH 作品没有页面')
    expected=payload.get('num_pages')
    if expected is not None and int(expected)!=len(paths): raise PartialDownloadError('NH 页数不一致')
    return name,paths

class NhImageSource:
    source='NH'
    normalize_id=staticmethod(normalize_nh_id)
    def __init__(self,settings): self.settings=settings

    async def fetch(self,album_id,directory):
        import aiohttp
        s=self.settings
        timeout=aiohttp.ClientTimeout(total=s.request_timeout)
        headers={'User-Agent':'Mozilla/5.0','Referer':f'https://nhentai.net/g/{album_id}/'}
        async with aiohttp.ClientSession(timeout=timeout,headers=headers,trust_env=True) as session:
            payload=None
            for endpoint in (f'/api/v2/galleries/{album_id}',f'/api/gallery/{album_id}'):
                async with session.get('https://nhentai.net'+endpoint,allow_redirects=False) as response:
                    if response.status==404: continue
                    if response.status!=200: raise DownloadError(f'NH 元数据请求失败（HTTP {response.status}）')
                    raw=await self._read(response,4*1024*1024)
                    import json
                    try: payload=json.loads(raw)
                    except ValueError as exc: raise DownloadError('NH 返回非 JSON，可能被站点拦截') from exc
                    break
            if payload is None: raise AlbumNotFoundError('NH 编号不存在')
            title,paths=parse_gallery(payload)
            if len(paths)>s.max_pages: raise DownloadError('NH 页数超过配置上限')
            semaphore=asyncio.Semaphore(s.image_concurrency)
            total=0
            async def download_page(index,path):
                nonlocal total
                target=directory/f'{index:06d}{Path(path).suffix}'
                async with semaphore:
                    if target.is_file() and 0<target.stat().st_size<=s.max_image_bytes:
                        total+=target.stat().st_size
                        if total>s.max_total_bytes: raise DownloadError('NH 资源超过大小上限')
                        return target
                    for host in ('i1.nhentai.net','i2.nhentai.net','i3.nhentai.net','i4.nhentai.net'):
                        try:
                            async with session.get('https://'+host+path,allow_redirects=False) as response:
                                if response.status in (401,403): raise DownloadError(f'NH 图片访问受限（HTTP {response.status}）')
                                if response.status!=200: continue
                                raw=await self._read(response,s.max_image_bytes)
                            if not raw.startswith((b'\xff\xd8',b'\x89PNG',b'GIF8',b'RIFF')): continue
                            total+=len(raw)
                            if total>s.max_total_bytes: raise DownloadError('NH 资源超过大小上限')
                            part=target.with_suffix('.part')
                            part.write_bytes(raw); part.replace(target)
                            return target
                        except (aiohttp.ClientError,TimeoutError): continue
                    raise PartialDownloadError(f'NH 第 {index} 页下载失败')
            tasks=[asyncio.create_task(download_page(i,p)) for i,p in enumerate(paths,1)]
            try: images=await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done(): task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
            return ImageBundle(title,tuple(images))

    @staticmethod
    async def _read(response,limit):
        data=bytearray()
        async for chunk in response.content.iter_chunked(64*1024):
            data.extend(chunk)
            if len(data)>limit: raise DownloadError('NH 响应超过大小限制')
        return bytes(data)

class NhDownloadService(DownloadService):
    def __init__(self,settings,*,adapter=None,pdf_service=None):
        super().__init__(settings,adapter or NhImageSource(settings),pdf_service=pdf_service)
