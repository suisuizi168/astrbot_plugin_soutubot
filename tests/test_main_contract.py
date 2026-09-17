from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class Image:
    def __init__(self, file=None, **kwargs) -> None:
        self.file = file
        self.url = kwargs.get("url", "")
        self.path = kwargs.get("path", "")

    async def convert_to_file_path(self):
        return self.path or self.file

    @staticmethod
    def fromURL(url: str):
        return Image(file=url)


class Reply:
    def __init__(self, chain=None, reply_id="reply-1") -> None:
        self.chain = chain
        self.id = reply_id


class Plain:
    def __init__(self, text: str) -> None:
        self.text = text


class MessageChain(list):
    pass


class AstrMessageEvent:
    pass


class Context:
    pass


class Star:
    def __init__(self, context) -> None:
        self.context = context


class Filter:
    @staticmethod
    def command(*args, **kwargs):
        return lambda function: function

    @staticmethod
    def llm_tool(*args, **kwargs):
        return lambda function: function


class Logger:
    def debug(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass

    def error(self, *args, **kwargs) -> None:
        pass


components = types.ModuleType("astrbot.api.message_components")
components.Image = Image
components.Reply = Reply
components.Plain = Plain

api = types.ModuleType("astrbot.api")
api.AstrBotConfig = dict
api.logger = Logger()

event = types.ModuleType("astrbot.api.event")
event.AstrMessageEvent = AstrMessageEvent
event.MessageChain = MessageChain
event.filter = Filter()

star = types.ModuleType("astrbot.api.star")
star.Context = Context
star.Star = Star

astrbot = types.ModuleType("astrbot")
astrbot_api = types.ModuleType("astrbot.api")
astrbot_api.__path__ = []
astrbot_api.AstrBotConfig = dict
astrbot_api.logger = api.logger

sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", astrbot_api)
sys.modules.setdefault("astrbot.api.message_components", components)
sys.modules.setdefault("astrbot.api.event", event)
sys.modules.setdefault("astrbot.api.star", star)

PACKAGE_NAME = Path(__file__).resolve().parents[1].name
main_module = importlib.import_module(f"{PACKAGE_NAME}.main")
models_module = importlib.import_module(f"{PACKAGE_NAME}.models")
Main = main_module.Main


class Event:
    def __init__(self, messages) -> None:
        self._messages = messages
        self.unified_msg_origin = "aiocqhttp:group:123"

    def get_messages(self):
        return self._messages


class MainContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_image_has_priority(self) -> None:
        direct = Image()
        quoted = Image()
        found = await Main._extract_image(Event([Reply([quoted]), direct]))
        self.assertIs(found, direct)

    async def test_embedded_quoted_image_is_supported(self) -> None:
        quoted = Image()
        found = await Main._extract_image(Event([Reply([quoted])]))
        self.assertIs(found, quoted)

    async def test_quoted_image_falls_back_to_astrbot_resolver(self) -> None:
        async def resolver(event, reply):
            return ["https://img.example/quoted.jpg"]

        original = main_module.extract_quoted_message_images
        main_module.extract_quoted_message_images = resolver
        try:
            found = await Main._extract_image(Event([Reply(None)]))
        finally:
            main_module.extract_quoted_message_images = original
        self.assertIsInstance(found, Image)
        self.assertEqual(found.file, "https://img.example/quoted.jpg")

    async def test_missing_image_returns_none(self) -> None:
        self.assertIsNone(await Main._extract_image(Event([Plain("/搜本子")])))

    async def test_materialize_image_falls_back_from_stale_path_to_url(self) -> None:
        class Resolver:
            def __init__(self, ref, **kwargs) -> None:
                self.ref = ref

            async def to_bytes(self):
                if self.ref == "C:/missing/image.jpg":
                    raise FileNotFoundError
                return b"valid-image-bytes"

        image = Image(
            file="legacy-file-name",
            path="C:/missing/image.jpg",
            url="https://img.example/current.jpg",
        )
        original = main_module.MediaResolver
        main_module.MediaResolver = Resolver
        try:
            data, filename = await Main._materialize_image(Event([]), image)
        finally:
            main_module.MediaResolver = original
        self.assertEqual(data, b"valid-image-bytes")
        self.assertEqual(filename, "current.jpg")

    async def test_materialize_image_recovers_onebot_local_reference(self) -> None:
        class Resolver:
            def __init__(self, ref, **kwargs) -> None:
                self.ref = ref

            async def to_bytes(self):
                if self.ref == "C:/napcat/cache/missing.jpg":
                    raise FileNotFoundError
                if self.ref == "https://multimedia.example/recovered.jpg":
                    return b"recovered-image-bytes"
                raise AssertionError(f"unexpected ref: {self.ref}")

        class OneBotResolver:
            def __init__(self, event) -> None:
                self.event = event

            async def resolve_for_llm(self, refs):
                self.refs = refs
                return ["https://multimedia.example/recovered.jpg"]

        image = Image(
            file="C:/napcat/cache/missing.jpg",
            path="C:/napcat/cache/missing.jpg",
        )
        original_media = main_module.MediaResolver
        original_onebot = main_module.AstrBotImageResolver
        main_module.MediaResolver = Resolver
        main_module.AstrBotImageResolver = OneBotResolver
        try:
            data, filename = await Main._materialize_image(Event([]), image)
        finally:
            main_module.MediaResolver = original_media
            main_module.AstrBotImageResolver = original_onebot
        self.assertEqual(data, b"recovered-image-bytes")
        self.assertEqual(filename, "recovered.jpg")

    async def test_llm_tool_queues_background_search(self) -> None:
        plugin = object.__new__(Main)
        plugin._enable_llm_tool = True
        plugin._closing = False
        plugin._tasks = set()
        plugin._max_pending_tasks = 20
        plugin._strict_default = False
        plugin._claim_request = AsyncMock(return_value=(True, ""))
        plugin._extract_image = AsyncMock(return_value=Image(file="image-ref"))
        plugin._materialize_image = AsyncMock(return_value=(b"image", "image.jpg"))
        queued = []
        plugin._start_background_search = lambda **kwargs: queued.append(kwargs)

        result = await plugin.search_doujin_tool(Event([]), strict=True)

        self.assertIn("结果会直接发送到当前会话", result)
        self.assertEqual(len(queued), 1)
        self.assertTrue(queued[0]["strict"])

    async def test_result_chain_contains_only_high_similarity_images(self) -> None:
        plugin = object.__new__(Main)
        plugin._max_results = 3
        response = models_module.SearchResponse(
            result_id="private-result-id",
            image_url=None,
            items=(
                models_module.SearchItem(
                    score=91.0,
                    title="High",
                    source_key="nhentai",
                    source_name="NHentai",
                    thumbnail_url="https://img.example/high.jpg",
                ),
                models_module.SearchItem(
                    score=79.9,
                    title="Low",
                    source_key="nhentai",
                    source_name="NHentai",
                    thumbnail_url="https://img.example/low.jpg",
                ),
            ),
        )
        chain = plugin._build_result_chain(response)
        images = [segment for segment in chain if isinstance(segment, Image)]
        text = "".join(
            segment.text for segment in chain if isinstance(segment, Plain)
        )
        self.assertEqual([image.file for image in images], ["https://img.example/high.jpg"])
        self.assertIn("91.00%", text)
        self.assertNotIn("79.90%", text)
        self.assertNotIn("private-result-id", text)
        self.assertIn("来源于搜图Bot酱", text)


if __name__ == "__main__":
    unittest.main()
