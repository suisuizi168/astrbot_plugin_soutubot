"""Shared task/cache lifecycle. Adapters fetch images; PdfService creates PDFs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

if __package__:
    from .pdf_service import PdfService, PdfGenerationError
else:
    from pdf_service import PdfService, PdfGenerationError

logger = logging.getLogger(__name__)

class DownloadError(RuntimeError): pass
class AlbumNotFoundError(DownloadError): pass
class PartialDownloadError(DownloadError): pass
class DownloadTimeoutError(DownloadError): pass

@dataclass(frozen=True, slots=True)
class ImageBundle:
    title: str
    images: tuple[Path, ...]

@dataclass(frozen=True, slots=True)
class DownloadSettings:
    root_dir: Path
    timeout_seconds: int = 600
    max_concurrency: int = 1
    max_pending_tasks: int = 20
    pdf_cache_enabled: bool = True
    pdf_cache_ttl_hours: int = 168
    cleanup_images_after_pdf: bool = True
    pdf_password_enabled: bool = True
    pdf_fixed_password: str = ""

@dataclass(frozen=True, slots=True)
class DownloadArtifact:
    album_id: str
    title: str
    pdf_path: Path
    pdf_size: int
    cache_hit: bool
    duration: float
    password: str = ""

@dataclass(frozen=True, slots=True)
class DownloadAcquireResult:
    status: Literal["cache", "new", "joined"]
    artifact: DownloadArtifact | None = None
    task: asyncio.Task | None = None

    async def wait(self):
        if self.artifact is not None: return self.artifact
        if self.task is None: raise DownloadError("missing download task")
        return await asyncio.shield(self.task)

@dataclass(frozen=True, slots=True)
class CleanupResult:
    files_deleted: int
    bytes_freed: int
    skipped_active: int = 0

class DownloadService:
    def __init__(self, settings, adapter, *, pdf_service=None):
        self.settings, self.adapter = settings, adapter
        self.root = Path(settings.root_dir).resolve()
        self.pdf_dir, self.images_dir, self.work_dir = (self.root/n for n in ('pdf','images','work'))
        # No filesystem or optional dependency is needed merely to load a plugin.
        self.pdf_service = pdf_service or PdfService()
        self._active = {}
        self._leases = {}
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1,settings.max_concurrency))
        self._closed = False

    @property
    def active_ids(self): return frozenset(self._active) | frozenset(self._leases)

    def hold(self, album_id):
        self._leases[album_id] = self._leases.get(album_id, 0) + 1

    def release(self, album_id):
        count = self._leases.get(album_id, 0) - 1
        if count > 0: self._leases[album_id] = count
        else: self._leases.pop(album_id, None)

    def pdf_password(self, album_id):
        return (self.settings.pdf_fixed_password.strip() or album_id) if self.settings.pdf_password_enabled else ''

    def pdf_path(self, album_id):
        album_id=self.adapter.normalize_id(album_id)
        password=self.pdf_password(album_id)
        suffix='-enc-'+hashlib.sha256(password.encode()).hexdigest() if password else ''
        return self.pdf_dir / f'{album_id}{suffix}.pdf'

    def _valid_cached_pdf(self, path):
        try:
            ttl=self.settings.pdf_cache_ttl_hours
            if not path.is_file() or (ttl>0 and time.time()-path.stat().st_mtime>ttl*3600): return False
            with path.open('rb') as stream: return stream.read(5)==b'%PDF-'
        except OSError: return False

    async def acquire(self, album_id, *, trace_id='internal'):
        album_id=self.adapter.normalize_id(album_id)
        async with self._lock:
            if self._closed: raise DownloadError('service closed')
            task=self._active.get(album_id)
            if task is not None: return DownloadAcquireResult('joined', task=task)
            cached=self.pdf_path(album_id)
            if self.settings.pdf_cache_enabled and self._valid_cached_pdf(cached):
                return DownloadAcquireResult('cache', artifact=DownloadArtifact(album_id,'',cached,cached.stat().st_size,True,0,self.pdf_password(album_id)))
            if len(self._active)>=self.settings.max_pending_tasks: raise DownloadError('待处理下载任务已满')
            task=asyncio.create_task(self._download(album_id,trace_id=trace_id))
            self._active[album_id]=task
            task.add_done_callback(lambda done: self._finished(album_id,done))
            return DownloadAcquireResult('new',task=task)

    def _finished(self, album_id, task):
        if self._active.get(album_id) is task: self._active.pop(album_id,None)
        if not task.cancelled(): task.exception()  # retrieve even if all waiters left

    async def get_or_download(self, album_id):
        return await (await self.acquire(album_id)).wait()

    def _load_bundle(self, directory):
        try:
            data=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
            paths=tuple((directory/name).resolve() for name in data['images'])
            root=directory.resolve()
            if not paths or any(root not in p.parents or not p.is_file() or p.stat().st_size==0 for p in paths): return None
            return ImageBundle(str(data['title']),paths)
        except (OSError,ValueError,KeyError,TypeError): return None

    def _save_bundle(self, directory, bundle):
        root=directory.resolve()
        if not bundle.images: raise PartialDownloadError('没有完整图片')
        names=[]
        for image in bundle.images:
            path=Path(image).resolve()
            if root not in path.parents or not path.is_file() or path.stat().st_size==0:
                raise PartialDownloadError('下载图片清单不完整')
            names.append(str(path.relative_to(root)))
        temp=directory/'manifest.tmp'
        temp.write_text(json.dumps({'title':bundle.title,'images':names},ensure_ascii=False),encoding='utf-8')
        temp.replace(directory/'manifest.json')

    async def _download(self, album_id, *, trace_id='internal'):
        started=time.monotonic()
        directory=self.images_dir/album_id
        work=self.work_dir/f'{album_id}-{uuid.uuid4().hex}'
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                async with self._semaphore:
                    directory.mkdir(parents=True,exist_ok=True)
                    work.mkdir(parents=True,exist_ok=True)
                    bundle=self._load_bundle(directory)
                    if bundle is None:
                        bundle=await self.adapter.fetch(album_id,directory)
                        self._save_bundle(directory,bundle)
                    logger.info('[下载:%s] %s%s 图片就绪，生成 PDF',trace_id,self.adapter.source,album_id)
                    password=self.pdf_password(album_id)
                    generated=work/'ready.pdf'
                    await self.pdf_service.build(bundle.images,generated,password)
                    target=self.pdf_path(album_id)
                    target.parent.mkdir(parents=True,exist_ok=True)
                    generated.replace(target)
                    if self.settings.cleanup_images_after_pdf:
                        try: self._remove_tree_inside_root(directory)
                        except OSError: logger.warning('PDF 已生成，图片清理失败')
                    return DownloadArtifact(album_id,bundle.title,target,target.stat().st_size,False,time.monotonic()-started,password)
        except TimeoutError as exc: raise DownloadTimeoutError('下载或 PDF 处理超时') from exc
        finally:
            if work.exists(): self._remove_tree_inside_root(work)

    def _remove_tree_inside_root(self,path):
        resolved=path.resolve()
        if self.root not in resolved.parents: raise ValueError('unsafe cache path')
        if resolved.exists(): shutil.rmtree(resolved)

    def clean_all(self):
        deleted=freed=0; skipped=set()
        for root in (self.images_dir,self.pdf_dir):
            if not root.exists(): continue
            for path in root.iterdir():
                key=path.stem.split('-enc-',1)[0] if root==self.pdf_dir else path.name
                if key in self.active_ids: skipped.add(key); continue
                resolved=path.resolve()
                if self.root not in resolved.parents: raise ValueError('unsafe cache path')
                files=[resolved] if resolved.is_file() else list(resolved.rglob('*'))
                for f in files:
                    if f.is_file(): freed+=f.stat().st_size; deleted+=1
                if resolved.is_dir(): shutil.rmtree(resolved)
                else: resolved.unlink(missing_ok=True)
        return CleanupResult(deleted,freed,len(skipped))

    async def close(self):
        self._closed=True
        tasks=tuple(self._active.values())
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks,return_exceptions=True)
        self._active.clear()
