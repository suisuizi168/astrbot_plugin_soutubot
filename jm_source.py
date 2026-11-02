"""JM image downloader; knows nothing about search, PDF generation or messaging."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from .download_service import DownloadSettings, ImageBundle, DownloadError, AlbumNotFoundError, PartialDownloadError
else:
    from download_service import DownloadSettings, ImageBundle, DownloadError, AlbumNotFoundError, PartialDownloadError

def normalize_jm_id(value):
    text=str(value or '').strip()
    if re.fullmatch(r'[0-9]{1,12}',text): return text
    if any(c in text for c in ('/','\\','..',':')): raise ValueError('invalid JM ID')
    match=re.search(r'(?i)(?:^|[^A-Za-z0-9])JM\s*([0-9]{1,12})(?:$|[^A-Za-z0-9])',text)
    if match: return match.group(1)
    raise ValueError('invalid JM ID')

@dataclass(frozen=True,slots=True)
class JmDownloadSettings(DownloadSettings):
    image_concurrency: int = 10
    photo_concurrency: int = 2
    retry_times: int = 0
    domain_list: tuple[str,...] = ()
    proxy: str = 'system'

class JmImageSource:
    source='JM'
    normalize_id=staticmethod(normalize_jm_id)

    def __init__(self,settings,*,downloader=None,option_factory=None):
        self.settings=settings
        self.downloader=downloader
        self.option_factory=option_factory

    async def fetch(self,album_id,directory):
        try:
            downloader=self.downloader
            factory=self.option_factory
            if downloader is None or factory is None:
                from jmcomic import JmOption, download_album_async
                downloader=downloader or download_album_async
                factory=factory or JmOption.construct
            s=self.settings
            option=factory({
                'log':False,
                'client':{'impl':'api','retry_times':s.retry_times,'timeout':min(60,s.timeout_seconds),
                          'domain':{'api':list(s.domain_list)},'postman':{'meta_data':{'proxies':s.proxy}}},
                'download':{'cache':True,'image':{'decode':True},
                            'threading':{'image':s.image_concurrency,'photo':s.photo_concurrency}},
                'dir_rule':{'base_dir':str(directory),'rule':'Bd / Aid / Pindex'},
            })
            result=await downloader(album_id,option,check_exception=True)
            # jmcomic's manifest is ordered by chapter.index then image.index.
            images=tuple(Path(p) for p in getattr(getattr(result,'manifest',None),'image_filepath_list',()))
            if not images: raise PartialDownloadError('JM 未返回完整图片清单')
            detail=getattr(result,'detail',None) or getattr(result,'album',None)
            title=str(getattr(detail,'name','') or getattr(detail,'title','') or album_id)
            return ImageBundle(title,images)
        except Exception as exc:
            name=type(exc).__name__
            if name=='MissingAlbumPhotoException': raise AlbumNotFoundError() from exc
            if name=='PartialDownloadFailedException': raise PartialDownloadError() from exc
            if isinstance(exc,DownloadError): raise
            raise DownloadError('JM 图片下载失败（'+name+'）') from exc
