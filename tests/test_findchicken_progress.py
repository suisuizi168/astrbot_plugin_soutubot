"""Progress traces follow concurrent work without changing public replies."""

import asyncio
import re
import types
import unittest
from unittest.mock import AsyncMock, patch

import test_findchicken as fixtures

workflow = fixtures.workflow


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.WorkflowTests.asyncSetUp(self)
        self.plugin._google_lens.findchicken_enabled = False

    asyncTearDown = fixtures.WorkflowTests.asyncTearDown
    event = fixtures.WorkflowTests.event
    run_command = fixtures.WorkflowTests.run_command
    texts = fixtures.WorkflowTests.texts

    def logs(self, logger):
        return [call.args[0] % call.args[1:] for call in logger.mock_calls]

    async def test_whole_request_has_one_id_and_preserves_reply(self):
        with patch.object(workflow, "logger") as logger:
            await self.run_command()
        lines = self.logs(logger)
        joined = "\n".join(lines)
        self.assertEqual(len(set(re.findall(r"\[找鸡:([a-f0-9]{8})\]", joined))), 1)
        phases = [
            "开始处理",
            "等待查询名额",
            "开始获取",
            "图片准备完成",
            "开始并行搜索",
            "查询完成",
            "搜索汇总",
            "候选整理完成",
            "匹配来源兜底",
            "开始发送结果",
            "结果发送完成",
            "请求结束",
        ]
        positions = [
            next(i for i, line in enumerate(lines) if phase in line) for phase in phases
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(self.texts()[0], "收到，开始找鸡")
        self.assertIn("找到匹配来源", self.texts()[-1])
        self.assertNotIn("test-chicken-key", joined)
        self.assertNotIn("https://", joined)

    async def test_empty_and_error_are_distinct_and_exception_body_is_private(self):
        self.plugin._findchicken_client.search.return_value = {"results": []}
        with patch.object(workflow, "logger") as logger:
            await self.run_command()
        self.assertEqual(self.texts()[-1], workflow.FINDCHICKEN_EMPTY)
        self.assertIn("无可展示候选", "\n".join(self.logs(logger)))
        self.plugin._findchicken_client.search.side_effect = RuntimeError(
            "secret-url-and-key"
        )
        with patch.object(workflow, "logger") as logger:
            await self.run_command()
        joined = "\n".join(self.logs(logger))
        self.assertEqual(self.texts()[-1], workflow.FINDCHICKEN_ERROR)
        self.assertIn("查询失败", joined)
        self.assertIn("RuntimeError", joined)
        self.assertNotIn("secret-url-and-key", joined)

    async def test_fast_provider_completion_is_visible_before_slow_one(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return types.SimpleNamespace(results=[])

        self.plugin._google_lens.findchicken_enabled = True
        self.plugin._google_lens.search = AsyncMock(side_effect=slow)
        with patch.object(workflow, "logger") as logger:
            task = asyncio.create_task(
                self.plugin._query_findchicken(
                    None, "https://private.test/a", True, trace_id="1234abcd"
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 1)
                for _ in range(10):
                    await asyncio.sleep(0)
                    if "查询完成：SauceNAO" in "\n".join(self.logs(logger)):
                        break
                joined = "\n".join(self.logs(logger))
                self.assertIn("查询完成：SauceNAO", joined)
                self.assertNotIn("查询完成：Google Lens", joined)
                self.assertFalse(task.done())
            finally:
                release.set()
                await task

    async def test_llm_error_logs_fallback_and_keeps_result(self):
        self.plugin._findchicken_llm_enabled = True
        with (
            patch.object(workflow, "logger") as logger,
            patch.object(workflow, "enrich_identities", new=AsyncMock()),
            patch.object(
                workflow,
                "judge_author",
                new=AsyncMock(side_effect=RuntimeError("private")),
            ),
        ):
            await self.run_command()
        joined = "\n".join(self.logs(logger))
        self.assertIn("开始 LLM 判断", joined)
        self.assertIn("LLM 判断失败", joined)
        self.assertIn("匹配来源兜底", joined)
        self.assertIn("找到匹配来源", self.texts()[-1])
        self.assertNotIn("private", joined)

    async def test_cancellation_logs_stage_and_releases_slot(self):
        entered = asyncio.Event()

        async def wait_input(event):
            entered.set()
            await asyncio.Event().wait()

        self.plugin._findchicken_input = wait_input
        with patch.object(workflow, "logger") as logger:
            task = asyncio.create_task(
                self.plugin._background_findchicken(self.event(), "key")
            )
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        joined = "\n".join(self.logs(logger))
        self.assertIn("任务取消：阶段=准备图片", joined)
        self.assertIn("请求结束：已取消", joined)
        self.assertFalse(self.plugin._findchicken_semaphore.locked())
        self.assertEqual(self.texts(), ["收到，开始找鸡"])
