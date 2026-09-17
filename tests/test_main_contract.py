from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class Image:
    pass


class Reply:
    def __init__(self, chain=None) -> None:
        self.chain = chain


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
Main = importlib.import_module(f"{PACKAGE_NAME}.main").Main


class Event:
    def __init__(self, messages) -> None:
        self._messages = messages

    def get_messages(self):
        return self._messages


class MainContractTests(unittest.TestCase):
    def test_direct_image_has_priority(self) -> None:
        direct = Image()
        quoted = Image()
        found = Main._extract_image(Event([Reply([quoted]), direct]))
        self.assertIs(found, direct)

    def test_quoted_image_is_supported(self) -> None:
        quoted = Image()
        found = Main._extract_image(Event([Reply([quoted])]))
        self.assertIs(found, quoted)

    def test_missing_image_returns_none(self) -> None:
        self.assertIsNone(Main._extract_image(Event([Plain("/搜本子")])))


if __name__ == "__main__":
    unittest.main()
