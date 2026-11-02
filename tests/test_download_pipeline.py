from __future__ import annotations
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
import pikepdf
from download_service import (DownloadService, DownloadSettings, ImageBundle, DownloadError,
                              DownloadTimeoutError, PartialDownloadError, PdfGenerationError)
from jm_source import JmImageSource, JmDownloadSettings, normalize_jm_id
from nh_downloader import NhDownloadService, NhDownloadSettings, normalize_nh_id, parse_gallery, NhImageSource


class FakeSource:
    source='JM'
    normalize_id=staticmethod(normalize_jm_id)
    def __init__(self): self.calls=0; self.active=0; self.maximum=0; self.fail=False
    async def fetch(self,album_id,directory):
        self.calls+=1; self.active+=1; self.maximum=max(self.maximum,self.active)
        try:
            await asyncio.sleep(.02)
            paths=[]
            for i in range(2):
                path=directory/f'{i}.png'
                Image.new('RGB',(20+i,30+i),(i*100,0,0)).save(path)
                paths.append(path)
            if self.fail: raise PartialDownloadError('missing page')
            return ImageBundle('Title',tuple(paths))
        finally: self.active-=1


class DownloadPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.source=FakeSource(); self.services=[]

    def service(self,source=None,subdir='jm',**settings):
        service=DownloadService(DownloadSettings(Path(self.temp.name)/subdir,**settings),source or self.source)
        self.services.append(service); return service

    async def asyncTearDown(self):
        for service in self.services: await service.close()

    async def test_same_id_is_downloaded_and_converted_once_then_cached(self):
        service=self.service()
        artifacts=await asyncio.gather(*(service.get_or_download('123') for _ in range(8)))
        self.assertEqual(self.source.calls,1)
        self.assertEqual(len({a.pdf_path for a in artifacts}),1)
        with pikepdf.open(artifacts[0].pdf_path,password='123') as pdf:
            self.assertEqual(len(pdf.pages),2); self.assertTrue(pdf.is_encrypted)
        self.assertEqual((await service.acquire('123')).status,'cache')
        self.assertFalse((service.images_dir/'123').exists())

    async def test_different_ids_obey_processing_limit(self):
        service=self.service(max_concurrency=1)
        await asyncio.gather(*(service.get_or_download(i) for i in ('1','2','3')))
        self.assertEqual(self.source.maximum,1)

    async def test_nh_and_jm_same_number_never_share_cache(self):
        jm=self.service(subdir='jm'); nh=self.service(subdir='nh')
        a,b=await asyncio.gather(jm.get_or_download('123'),nh.get_or_download('123'))
        self.assertNotEqual(a.pdf_path,b.pdf_path); self.assertEqual(self.source.calls,2)

    async def test_pdf_password_modes_keep_distinct_cache(self):
        paths=[]
        for enabled,fixed,password in ((False,'',''),(True,'','123'),(True,'fixed','fixed')):
            s=self.service(pdf_password_enabled=enabled,pdf_fixed_password=fixed)
            artifact=await s.get_or_download('123'); paths.append(artifact.pdf_path)
            with pikepdf.open(artifact.pdf_path,password=password) as pdf:
                self.assertEqual(pdf.is_encrypted,enabled)
            self.assertEqual(artifact.password,password)
        self.assertEqual(len(set(paths)),3)

    async def test_pdf_failure_keeps_images_and_retry_does_not_download(self):
        service=self.service()
        original=service.pdf_service
        async def fail(*args): raise PdfGenerationError()
        service.pdf_service=SimpleNamespace(build=fail)
        with self.assertRaises(PdfGenerationError): await service.get_or_download('123')
        self.assertTrue((service.images_dir/'123'/'manifest.json').exists())
        self.assertFalse(service.pdf_path('123').exists())
        service.pdf_service=original
        await service.get_or_download('123')
        self.assertEqual(self.source.calls,1)

    async def test_incomplete_download_does_not_generate_pdf_or_publish_manifest(self):
        self.source.fail=True; s=self.service()
        with self.assertRaises(PartialDownloadError): await s.get_or_download('123')
        self.assertFalse((s.images_dir/'123'/'manifest.json').exists())
        self.assertFalse(s.pdf_path('123').exists())

    async def test_corrupt_image_fails_pdf_and_preserves_inputs(self):
        async def corrupt(identifier,directory):
            p=directory/'page.jpg'; p.write_bytes(b'not-an-image')
            return ImageBundle('Broken',(p,))
        self.source.fetch=corrupt; s=self.service()
        with self.assertRaises(PdfGenerationError): await s.get_or_download('123')
        self.assertTrue((s.images_dir/'123'/'page.jpg').exists())
        self.assertFalse(s.pdf_path('123').exists())

    async def test_timeout_releases_task_for_retry(self):
        s=self.service(timeout_seconds=.001)
        with self.assertRaises(DownloadTimeoutError): await s.get_or_download('123')
        await asyncio.sleep(0)
        self.assertNotIn('123',s.active_ids)

    async def test_pending_limit_and_close(self):
        s=self.service(max_pending_tasks=1)
        first=await s.acquire('123')
        with self.assertRaises(DownloadError): await s.acquire('456')
        self.assertEqual((await s.acquire('123')).status,'joined')
        await s.close()
        self.assertTrue(first.task.cancelled())
        with self.assertRaises(DownloadError): await s.acquire('123')

    async def test_cleanup_skips_sending_files_until_release(self):
        s=self.service(); artifact=await s.get_or_download('123')
        s.hold('123')
        self.assertEqual(s.clean_all().skipped_active,1)
        self.assertTrue(artifact.pdf_path.exists())
        s.release('123'); self.assertEqual(s.clean_all().files_deleted,1)

    async def test_jm_adapter_uses_image_manifest_without_pdf_export_feature(self):
        observed={}
        async def downloader(identifier,option,*,check_exception):
            observed.update(option)
            directory=Path(option['dir_rule']['base_dir']); directory.mkdir(parents=True,exist_ok=True)
            a=directory/'chapter2-1.jpg'; b=directory/'chapter1-2.jpg'
            a.write_bytes(b'a'); b.write_bytes(b'b')
            return SimpleNamespace(detail=SimpleNamespace(name='Test'),manifest=SimpleNamespace(image_filepath_list=[str(b),str(a)]))
        adapter=JmImageSource(JmDownloadSettings(Path(self.temp.name)),downloader=downloader,option_factory=lambda x:x)
        bundle=await adapter.fetch('123',Path(self.temp.name)/'images')
        self.assertEqual([p.name for p in bundle.images],['chapter1-2.jpg','chapter2-1.jpg'])
        self.assertNotIn('plugins',observed)

    async def test_pdf_cancellation_waits_for_writer_before_cleanup(self):
        from pdf_service import PdfService
        import threading
        entered=threading.Event(); release=threading.Event()
        service=PdfService()
        def writer(*args): entered.set(); release.wait(2)
        with patch.object(service,'_build',writer):
            task=asyncio.create_task(service.build((),Path(self.temp.name)/'x.pdf'))
            await asyncio.to_thread(entered.wait,1)
            task.cancel(); await asyncio.sleep(.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError): await task


class NhParsingTests(unittest.TestCase):
    def test_ids_and_url_allowlist(self):
        for text in ('123','NH123','https://nhentai.net/g/123/'):
            self.assertEqual(normalize_nh_id(text),'123')
        for text in ('../123','https://evil.test/g/123/','http://nhentai.net/g/123/','https://nhentai.net/g/123/?x=1','１２３'):
            with self.assertRaises(ValueError): normalize_nh_id(text)

    def test_v2_and_legacy_page_order(self):
        _,v2=parse_gallery({'title':{'pretty':'a'},'pages':[{'path':'galleries/10/2.jpg'},{'path':'galleries/10/1.png'}]})
        self.assertEqual(v2,['/galleries/10/2.jpg','/galleries/10/1.png'])
        _,old=parse_gallery({'title':{'english':'b'},'media_id':'10','images':{'pages':[{'t':'j'},{'t':'p'}]},'num_pages':2})
        self.assertEqual(old,['/galleries/10/1.jpg','/galleries/10/2.png'])

    def test_bad_paths_and_missing_pages_are_rejected(self):
        for path in ('https://evil.test/x.jpg','../../x.jpg','//evil.test/x.jpg'):
            with self.assertRaises(DownloadError): parse_gallery({'pages':[{'path':path}]})
        with self.assertRaises(PartialDownloadError):
            parse_gallery({'pages':[{'path':'galleries/1/1.jpg'}],'num_pages':2})
