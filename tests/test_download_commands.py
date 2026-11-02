import importlib
import itertools
import json
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock

import test_main_contract as contract
from test_entries_and_nh_transport import Event

commands = importlib.import_module(f'{contract.PACKAGE_NAME}.download_commands')


def command_forms():
    for name, prefix, spacing in itertools.product(
        ('jm', 'JM', 'Jm', 'jM', 'nh', 'NH', 'Nh', 'nH'), ('', '/'), ('', ' ')
    ):
        yield f'{prefix}{name}{spacing}123456', name.lower()


class DownloadCommandParserTests(unittest.TestCase):
    def test_all_case_space_and_prefix_combinations(self):
        for text, source in command_forms():
            with self.subTest(text=text):
                self.assertEqual(commands.parse_download_command(text), (source, '123456'))
                event = Event()
                event.message_str = text
                event.is_at_or_wake_command = False
                self.assertTrue(commands.ExplicitDownloadFilter().filter(event, {}))

    def test_whitespace_and_numeric_boundaries(self):
        for text in (' \t/jM\t123456\n', 'NH\u3000123456', '/ nh 123456'):
            self.assertEqual(commands.parse_download_command(text)[1], '123456')
        self.assertEqual(commands.parse_download_command('JM123456789012'), ('jm', '123456789012'))

    def test_prose_multiple_ids_and_invalid_inputs_do_not_match(self):
        for text in ('', '123456', 'jm', 'nh', '下载JM123456', '不要下载 nh123456',
                     'JM123456 是什么', 'jm123456 nh654321', 'jm 123 456',
                     '//jm123456', 'xjm123456', 'nh123456x', 'jm-123456',
                     'jm1234567890123', 'nh１２３４５６', 'jm../123', 'nh https://nhentai.net/g/123456/'):
            with self.subTest(text=text):
                self.assertIsNone(commands.parse_download_command(text))


class ExplicitDownloadEntryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = contract.Main(types.SimpleNamespace(get_using_provider_async=AsyncMock()), {
            'llm_tools_enabled': False, 'jm_llm_enabled': False,
        })
        self.plugin._is_allowed = lambda event: True

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def test_all_forms_stop_conversation_before_queue_even_with_llm_disabled(self):
        for text, source in command_forms():
            with self.subTest(text=text):
                event = Event()
                event.message_str = text
                async def queue(*args):
                    self.assertTrue(event.stopped)
                    return 'queued'
                self.plugin._queue_download = AsyncMock(side_effect=queue)
                results = [r.text async for r in self.plugin.explicit_download_command(event)]
                self.assertEqual(results, ['queued'])
                self.plugin._queue_download.assert_awaited_once_with(event.unified_msg_origin, '2', source, '123456')
        self.plugin.context.get_using_provider_async.assert_not_awaited()

    async def test_whitelist_denial_consumes_explicit_request_without_download(self):
        self.plugin._is_allowed = lambda event: False
        self.plugin._queue_download = AsyncMock()
        for text in ('Jm123456', '/nH 123456'):
            event = Event()
            event.message_str = text
            results = [r.text async for r in self.plugin.explicit_download_command(event)]
            self.assertTrue(event.stopped)
            self.assertIn('白名单', ''.join(results))
        self.plugin._queue_download.assert_not_awaited()

    async def test_source_disabled_is_respected_and_does_not_fall_through_to_llm(self):
        self.plugin._jm_download_enabled = self.plugin._nh_download_enabled = False
        self.plugin._jm_download_service.acquire = AsyncMock()
        self.plugin._nh_download_service.acquire = AsyncMock()
        for text in ('jm123456', 'NH123456'):
            event = Event()
            event.message_str = text
            results = [r.text async for r in self.plugin.explicit_download_command(event)]
            self.assertTrue(event.stopped)
            self.assertIn('关闭', ''.join(results))
        self.plugin._jm_download_service.acquire.assert_not_awaited()
        self.plugin._nh_download_service.acquire.assert_not_awaited()

    async def test_non_command_is_left_for_normal_handling(self):
        event = Event()
        event.message_str = '帮我下载 JM123456'
        self.plugin._queue_download = AsyncMock()
        self.assertEqual([r async for r in self.plugin.explicit_download_command(event)], [])
        self.assertFalse(event.stopped)
        self.plugin._queue_download.assert_not_awaited()

    async def test_nh_defaults_on_without_overwriting_explicit_off(self):
        schema = json.loads((Path(__file__).parents[1] / '_conf_schema.json').read_text(encoding='utf-8'))
        self.assertTrue(schema['nh']['items']['nh_download_enabled']['default'])
        self.assertTrue(self.plugin._nh_download_enabled)
        disabled = contract.Main(types.SimpleNamespace(), {'nh_download_enabled': False})
        try:
            self.assertFalse(disabled._nh_download_enabled)
        finally:
            await disabled.terminate()
