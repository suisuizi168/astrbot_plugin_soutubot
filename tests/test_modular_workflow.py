from __future__ import annotations
import asyncio
import importlib
import io
import types
import unittest
from unittest.mock import AsyncMock
from PIL import Image as PILImage
import test_main_contract as contract

Main=contract.Main
models=contract.models_module
routing=importlib.import_module(f'{contract.PACKAGE_NAME}.routing')
multi=importlib.import_module(f'{contract.PACKAGE_NAME}.multi_search')
formatter=importlib.import_module(f'{contract.PACKAGE_NAME}.engines.formatter')

def response(source='nhentai',identifier='123',score=95):
    return models.SearchResponse(None,None,(models.SearchItem(score,'Some title',source,source,identifier,hit_index=0),))


class RouteTests(unittest.TestCase):
    def test_switch_matrix(self):
        for nh,jm,source,expected in (
            (False,False,'nhentai','none'),(True,False,'nhentai','nh'),
            (True,False,'ehentai','none'),(False,True,'nhentai','query_jm'),
            (False,True,'jmcomic','jm'),(True,True,'nhentai','nh'),
            (True,True,'ehentai','query_jm'),(True,True,'jmcomic','jm')):
            with self.subTest(nh=nh,jm=jm,source=source):
                self.assertEqual(routing.choose_download_route(response(source),nh_enabled=nh,jm_enabled=jm).action,expected)

    def test_all_segments_retained_and_nh_preferred_within_same_hit(self):
        payload={'results':[{'score':95,'path_segments':[
            {'source_key':'ehentai','metadata':{'title':'Title'}},
            {'source_key':'nhentai','source_url':'https://nhentai.net/g/123/','metadata':{'title':'Title'}}]}]}
        r=models.parse_search_response(payload)
        self.assertEqual(len(r.items),2)
        self.assertEqual(routing.choose_download_route(r,nh_enabled=True,jm_enabled=True).album_id,'123')

    def test_empty_segments_not_a_match(self):
        self.assertFalse(models.parse_search_response({'results':[{'score':99,'path_segments':[]}]}).items)

    def test_do_not_switch_to_lower_scoring_unrelated_nh_work(self):
        r=models.SearchResponse(None,None,(
            models.SearchItem(95,'Target','ehentai','EH',hit_index=0),
            models.SearchItem(90,'Different','nhentai','NH','123',hit_index=1)))
        target=routing.target_response(r,80)
        self.assertEqual(len(target.items),1)
        self.assertEqual(routing.choose_download_route(target,nh_enabled=True,jm_enabled=True).action,'query_jm')

    def test_fake_nh_domain_cannot_become_download_target(self):
        item=models.SearchItem(95,'Title','nhentai','NH',source_url='https://nhentai.net.evil.test/g/123/')
        self.assertEqual(routing.source_id(item,'nh'),'')


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin=Main(types.SimpleNamespace(),{'nh_download_enabled':True,'cooldown_seconds':0})
        self.plugin._jm_result_message_settle_seconds=0
        self.plugin._send_chain=AsyncMock()
        self.plugin._queue_download=AsyncMock(return_value='Queued')
        self.plugin._search_bytes=AsyncMock(return_value=response())
        self.plugin._resolve_jm_for_response=AsyncMock(return_value=contract.main_module.JmResolution(
            'Title','matched',selected=contract.main_module.JmCandidate('456','Title',100)))
        self.plugin._multi_search.search=AsyncMock(return_value='MULTI RESULT')
        buffer=io.BytesIO(); PILImage.new('RGB',(5,5)).save(buffer,format='PNG'); self.image=buffer.getvalue()

    async def asyncTearDown(self): await self.plugin.terminate()

    async def run_search(self):
        await self.plugin._background_search(umo='p:GroupMessage:1',sender_id='2',image_bytes=self.image,filename='x.png',strict=False)

    def texts(self):
        return '\n'.join(getattr(x,'text','') for call in self.plugin._send_chain.call_args_list for x in call.args[1])

    async def test_qualified_nh_suppresses_multi_and_jm_query(self):
        await self.run_search()
        self.plugin._queue_download.assert_not_awaited()
        self.assertEqual(contract.main_module.JmDownloadReplyFilter.pending[('p:GroupMessage:1','2')].source, 'nh')
        self.assertEqual(contract.main_module.JmDownloadReplyFilter.pending[('p:GroupMessage:1','2')].album_id, '123')
        self.plugin._resolve_jm_for_response.assert_not_awaited()
        self.plugin._multi_search.search.assert_not_awaited()
        self.assertNotIn('MULTI RESULT',self.texts())

    async def test_eh_routes_through_jm_then_waits_for_confirmation(self):
        self.plugin._search_bytes.return_value=response('ehentai')
        await self.run_search()
        self.plugin._resolve_jm_for_response.assert_awaited_once()
        self.plugin._queue_download.assert_not_awaited()
        self.assertEqual(contract.main_module.JmDownloadReplyFilter.pending[('p:GroupMessage:1','2')].source, 'jm')
        self.assertEqual(contract.main_module.JmDownloadReplyFilter.pending[('p:GroupMessage:1','2')].album_id, '456')
        self.assertIn('10 分钟',self.texts())
        self.plugin._multi_search.search.assert_not_awaited()

    async def test_jm_only_with_nh_result_queries_jm(self):
        self.plugin._nh_download_enabled=False
        await self.run_search()
        self.plugin._queue_download.assert_not_awaited()
        self.assertEqual(contract.main_module.JmDownloadReplyFilter.pending[('p:GroupMessage:1','2')].source, 'jm')
        self.assertEqual(contract.main_module.JmDownloadReplyFilter.pending[('p:GroupMessage:1','2')].album_id, '456')

    async def test_nh_only_with_eh_stops_after_result(self):
        self.plugin._jm_download_enabled=False
        self.plugin._search_bytes.return_value=response('ehentai')
        await self.run_search()
        self.plugin._queue_download.assert_not_awaited()
        self.plugin._resolve_jm_for_response.assert_not_awaited()
        self.plugin._multi_search.search.assert_not_awaited()

    async def test_disabled_downloads_still_suppress_multi_for_qualified_hit(self):
        self.plugin._jm_download_enabled=self.plugin._nh_download_enabled=False
        await self.run_search()
        self.plugin._queue_download.assert_not_awaited()
        self.plugin._multi_search.search.assert_not_awaited()

    async def test_low_score_falls_back_only_when_multi_enabled(self):
        self.plugin._search_bytes.return_value=response(score=79)
        await self.run_search()
        self.plugin._multi_search.search.assert_awaited_once()
        self.assertIn('MULTI RESULT',self.texts())
        self.plugin._queue_download.assert_not_awaited()
        self.plugin._multi_search.search.reset_mock(); self.plugin._multi_search.enabled=False
        await self.run_search(); self.plugin._multi_search.search.assert_not_awaited()

    async def test_soutubot_failure_does_not_disable_multi(self):
        self.plugin._search_bytes.side_effect=RuntimeError('blocked')
        await self.run_search()
        self.plugin._multi_search.search.assert_awaited_once()

    async def test_jm_master_allows_both_lookup_and_direct_source_download(self):
        self.plugin._search_bytes.return_value=response('ehentai')
        await self.run_search()
        self.plugin._resolve_jm_for_response.assert_awaited_once()
        self.plugin._queue_download.assert_not_awaited()
        self.assertEqual(contract.main_module.JmDownloadReplyFilter.pending[('p:GroupMessage:1','2')].source, 'jm')
        self.assertEqual(contract.main_module.JmDownloadReplyFilter.pending[('p:GroupMessage:1','2')].album_id, '456')
        self.plugin._queue_download.reset_mock()
        self.plugin._search_bytes.return_value=response('jmcomic')
        await self.run_search()
        self.plugin._queue_download.assert_not_awaited()
        self.assertEqual(contract.main_module.JmDownloadReplyFilter.pending[('p:GroupMessage:1','2')].source, 'jm')
        self.assertEqual(contract.main_module.JmDownloadReplyFilter.pending[('p:GroupMessage:1','2')].album_id, '123')

    async def test_unconfirmed_jm_candidates_do_not_download(self):
        self.plugin._search_bytes.return_value=response('ehentai')
        self.plugin._resolve_jm_for_response.return_value=contract.main_module.JmResolution('Title','candidates')
        await self.run_search()
        self.plugin._queue_download.assert_not_awaited()

    async def test_slow_match_not_lost_to_fast_candidate(self):
        self.plugin._resolve_jm_queries=types.MethodType(Main._resolve_jm_queries,self.plugin)
        async def resolve(query,**kw):
            await asyncio.sleep(.001 if query=='candidate' else .02)
            c=contract.main_module.JmCandidate('1' if query=='candidate' else '2',query,65 if query=='candidate' else 100)
            return contract.main_module.JmResolution(query,'candidates',candidates=(c,)) if query=='candidate' else contract.main_module.JmResolution(query,'matched',selected=c)
        self.plugin._jm_resolver.resolve=resolve
        result=await self.plugin._resolve_jm_queries(('candidate','match'),trace_id='test',total_timeout_seconds=1)
        self.assertEqual(result.status,'matched'); self.assertEqual(result.selected.album_id,'2')


class ParallelProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_failure_isolation_and_cache(self):
        service=multi.MultiSearchService({'saucenao_api_key':'private-test-key'})
        started=0; ready=asyncio.Event()
        async def success(*a,**kw):
            nonlocal started
            started+=1
            if started==2: ready.set()
            await asyncio.wait_for(ready.wait(),1)
            return formatter.SourceOutcome(results=[formatter.SearchResult('Title','site','https://example.test/')])
        async def fail(*a,**kw):
            nonlocal started
            started+=1
            if started==2: ready.set()
            await asyncio.wait_for(ready.wait(),1)
            raise RuntimeError('https://example.test/?api_key=private-test-key')
        service._clients={'yandex':types.SimpleNamespace(search=AsyncMock(side_effect=success),close=AsyncMock()),
                          'saucenao':types.SimpleNamespace(search=AsyncMock(side_effect=fail),close=AsyncMock())}
        image=types.SimpleNamespace(data=b'test',filename='test.jpg',content_type='image/jpeg')
        text=await service.search(image)
        self.assertIn('Title',text); self.assertIn('RuntimeError',text)
        self.assertNotIn('private-test-key',text)
        await service.search(image)
        self.service_calls=service._clients['yandex'].search.await_count
        self.assertEqual(self.service_calls,1)
        await service.close()

    async def test_disabled_multi_never_initializes_clients(self):
        service=multi.MultiSearchService({'multi_search_enabled':False})
        self.assertEqual(await service.search(None),''); self.assertFalse(service._clients)
