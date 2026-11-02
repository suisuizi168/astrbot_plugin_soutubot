"""Configuration switches must govern real entry points and model calls."""
import types
import unittest
from unittest.mock import AsyncMock

import test_main_contract as contract
from test_entries_and_nh_transport import Event


class ConfigControlsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugins = []

    async def asyncTearDown(self):
        for plugin in self.plugins:
            await plugin.terminate()

    def plugin(self, config):
        context = types.SimpleNamespace(get_using_provider_async=AsyncMock())
        plugin = contract.Main(context, config)
        self.plugins.append(plugin)
        plugin._is_allowed = lambda event: True
        plugin._jm_result_message_settle_seconds = 0
        plugin._jm_notice_sequence_delay_seconds = 0
        return plugin

    async def test_llm_master_and_auxiliary_switches_both_gate_all_jm_model_calls(self):
        title = '[Circle] Original Title (Edition) [Tag]'
        for master, auxiliary in ((False, True), (True, False), (False, False)):
            with self.subTest(master=master, auxiliary=auxiliary):
                plugin = self.plugin({'llm_tools_enabled': master, 'jm_llm_enabled': auxiliary})
                plan = await plugin._extract_jm_plan((title,), trace_id='test')
                self.assertEqual(plan.queries, (title,))
                self.assertEqual(plan.authors, ())
                candidate = contract.main_module.JmCandidate('123', title, 70)
                plugin._jm_resolver.resolve = AsyncMock(return_value=contract.main_module.JmResolution(
                    title, 'candidates', candidates=(candidate,)))
                response = contract.models_module.SearchResponse(None, None, (
                    contract.models_module.SearchItem(100, title, 'ehentai', 'EH'),))
                result = await plugin._resolve_jm_for_response(response)
                self.assertEqual(result.status, 'candidates')
                plugin._jm_resolver.resolve.assert_awaited_once_with(title, total_timeout_seconds=20)
                plugin.context.get_using_provider_async.assert_not_awaited()

    async def test_llm_remains_available_when_both_switches_enabled(self):
        plugin = self.plugin({'llm_tools_enabled': True, 'jm_llm_enabled': True})
        provider = types.SimpleNamespace(text_chat=AsyncMock(return_value=types.SimpleNamespace(
            completion_text='{"queries":["作品名称"],"authors":[]}')))
        plugin.context.get_using_provider_async.return_value = provider
        await plugin._extract_jm_plan(('Original Title',), trace_id='test')
        provider.text_chat.assert_awaited_once()

    async def test_jm_master_blocks_commands_tools_and_direct_result_shortcut(self):
        plugin = self.plugin({'jm_enabled': False, 'llm_tools_enabled': True})
        plugin._claim_request = AsyncMock(return_value=(True, ''))
        plugin._background_manual_jm_search = AsyncMock()
        plugin._jm_download_service.acquire = AsyncMock()
        event = Event()
        for method, message, args in (
            ('search_jm_title_command', '/搜本 Original Title', ()),
            ('jm_download_command', '/jm 123', ()),
            ('jm_search_tool', '', ('Original Title',)),
            ('jm_download_tool', '', ('123',)),
        ):
            with self.subTest(method=method):
                event.message_str = message
                results = [r.text async for r in getattr(plugin, method)(event, *args)]
                self.assertIn('关闭', ''.join(results))
        response = contract.models_module.SearchResponse(None, None, (
            contract.models_module.SearchItem(100, 'Title', 'jmcomic', 'JM', '123'),))
        self.assertEqual((await plugin._resolve_jm_for_response(response)).reason, 'disabled')
        plugin._claim_request.assert_not_awaited()
        plugin._background_manual_jm_search.assert_not_awaited()
        plugin._jm_download_service.acquire.assert_not_awaited()
        plugin.context.get_using_provider_async.assert_not_awaited()

    async def test_obsolete_jm_flags_and_timeouts_no_longer_override_current_controls(self):
        for enabled in (False, True):
            plugin = self.plugin({
                'jm_enabled': enabled, 'jm_download_enabled': not enabled,
                'search_jm_enabled': not enabled, 'jm_timeout_seconds': 2,
                'jm_query_timeout_seconds': 60, 'jm_author_query_timeout_seconds': 6,
            })
            self.assertEqual(plugin._jm_settings.enabled, enabled)
            self.assertEqual(plugin._jm_download_enabled, enabled)
            self.assertFalse(hasattr(plugin, '_search_jm_enabled'))
            self.assertEqual(plugin._jm_settings.timeout_seconds, 20)
            self.assertEqual(plugin._jm_query_timeout, 20)
            self.assertEqual(plugin._jm_author_query_timeout, 20)

    async def test_jm_disabled_leaves_explicit_nh_download_enabled(self):
        plugin = self.plugin({'jm_enabled': False, 'nh_download_enabled': True})
        plugin._queue_download = AsyncMock(return_value='NH queued')
        event = Event()
        event.message_str = '/nh 123'
        self.assertEqual([r.text async for r in plugin.nh_download_command(event)], ['NH queued'])
        plugin._queue_download.assert_awaited_once_with(event.unified_msg_origin, '2', 'nh', '123')

    async def test_pdf_settings_still_respect_existing_keys_after_reordering(self):
        plugin = self.plugin({
            'jm_pdf_password_enabled': True, 'jm_pdf_fixed_password': 'jm-test-password',
            'nh_pdf_password_enabled': True, 'nh_pdf_fixed_password': 'nh-test-password',
            'jm_pdf_cache_ttl_hours': 12, 'jm_max_pdf_size_mb': 10, 'nh_max_pdf_size_mb': 20,
        })
        self.assertEqual(plugin._jm_download_service.pdf_password('123'), 'jm-test-password')
        self.assertEqual(plugin._nh_download_service.pdf_password('123'), 'nh-test-password')
        self.assertEqual(plugin._jm_download_service.settings.pdf_cache_ttl_hours, 12)
        self.assertEqual(plugin._jm_max_pdf_size, 10 * 1024 * 1024)
        self.assertEqual(plugin._nh_max_pdf_size, 20 * 1024 * 1024)
