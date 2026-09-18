from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class Image:
    def __init__(self, file=None, **kwargs) -> None:
        self.file = file

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

class Logger:
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
        self.assertIsNone(await Main._extract_image(Event([Plain("/找本")])))

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
