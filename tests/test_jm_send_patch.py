from __future__ import annotations

import asyncio
import importlib
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import test_main_contract as contract

delivery = importlib.import_module(f"{contract.PACKAGE_NAME}.file_delivery")


class Rejected(Exception):
    def __init__(self, wording="group rejected", retcode=1200):
        self.result = {"status": "failed", "retcode": retcode, "wording": wording}


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.pdf = Path(self.directory.name) / "test file.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\nfixture")
        self.calls = []
        self.options = dict(
            umo="test:GroupMessage:100", pdf=self.pdf, jm_id="123456", sender_id="200",
            notice="title\npassword: test-only",
        )
        self.rejection_patch = patch.object(delivery, "ActionFailed", Rejected)
        self.rejection_patch.start()
        self.addCleanup(self.rejection_patch.stop)

    async def send(self, session, chain):
        self.calls.append((session, chain))

    def files(self):
        return [(session, component) for session, chain in self.calls for component in chain if isinstance(component, contract.File)]

    async def test_success_sends_notice_then_one_file(self):
        self.assertEqual(await delivery.send_jm_pdf(send=self.send, **self.options), (True, ""))
        self.assertEqual(len(self.calls), 2)
        self.assertIsInstance(self.calls[0][1][0], contract.Plain)
        self.assertEqual(self.files()[0][1].file, str(self.pdf))

    async def test_paths_preserve_astrbot_conversion(self):
        for value in (self.pdf.as_uri(), "relative.pdf"):
            self.calls.clear()
            result = await delivery.send_jm_pdf(send=self.send, **{**self.options, "pdf": value})
            self.assertTrue(result[0])
            self.assertEqual(self.files()[0][1].file, value)

    async def test_stat_permission_or_race_does_not_block_send(self):
        for error in (PermissionError("denied"), FileNotFoundError("race")):
            self.calls.clear()
            with patch.object(delivery.os.path, "getsize", side_effect=error):
                result = await delivery.send_jm_pdf(send=self.send, **self.options)
            self.assertTrue(result[0])
            self.assertEqual(len(self.files()), 1)

    async def test_invalid_paths_do_not_send(self):
        for value in (None, "", "  ", object()):
            self.assertFalse((await delivery.send_jm_pdf(send=self.send, **{**self.options, "pdf": value}))[0])
        self.assertEqual(self.calls, [])

    async def test_unknown_file_outcome_does_not_retry_or_fallback(self):
        for error in (TimeoutError("response lost"), ConnectionError("disconnected"), RuntimeError("unknown"), Rejected(retcode=1), Rejected("upload timeout")):
            self.calls.clear()
            async def send(session, chain):
                await self.send(session, chain)
                if isinstance(chain[0], contract.File):
                    raise error
            result = await delivery.send_jm_pdf(send=send, **self.options)
            self.assertFalse(result[0])
            self.assertEqual(len(self.files()), 1)
            self.assertTrue(all(session == self.options["umo"] for session, _ in self.calls))

    async def test_explicit_rejection_falls_back_with_password_and_group_notice(self):
        async def send(session, chain):
            await self.send(session, chain)
            if session == self.options["umo"] and isinstance(chain[0], contract.File):
                raise Rejected()
        self.assertTrue((await delivery.send_jm_pdf(send=send, **self.options))[0])
        private = [chain for session, chain in self.calls if session == "test:FriendMessage:200"]
        self.assertEqual(len(private), 2)
        self.assertIn("test-only", private[0][0].text)
        self.assertIsInstance(private[1][0], contract.File)
        self.assertIn("已通过私聊发送", self.calls[-1][1][0].text)

    async def test_failed_group_notice_can_fallback_before_file_attempt(self):
        async def send(session, chain):
            await self.send(session, chain)
            if session == self.options["umo"]:
                raise TimeoutError("text notice failed")
        self.assertTrue((await delivery.send_jm_pdf(send=send, **self.options))[0])
        self.assertEqual([session for session, _ in self.files()], ["test:FriendMessage:200"])

    async def test_private_failure_reports_both_errors_without_loop(self):
        async def send(session, chain):
            await self.send(session, chain)
            if isinstance(chain[0], contract.File):
                raise Rejected("private rejected" if ":FriendMessage:" in session else "group rejected")
        result = await delivery.send_jm_pdf(send=send, **self.options)
        self.assertFalse(result[0])
        self.assertIn("group rejected", result[1])
        self.assertIn("private rejected", result[1])
        self.assertEqual(len(self.files()), 2)

    async def test_no_fallback_for_private_invalid_sender_or_disabled(self):
        for overrides in (
            {"umo": "test:FriendMessage:200"}, {"umo": "malformed"}, {"sender_id": ""},
            {"sender_id": "200:extra"}, {"private_fallback": False},
        ):
            self.calls.clear()
            async def send(session, chain):
                await self.send(session, chain)
                raise Rejected()
            self.assertFalse((await delivery.send_jm_pdf(send=send, **{**self.options, **overrides}))[0])
            self.assertEqual(len(self.calls), 1)

    async def test_cancellation_propagates_without_more_sends(self):
        async def send(session, chain):
            await self.send(session, chain)
            raise asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await delivery.send_jm_pdf(send=send, **self.options)
        self.assertEqual(len(self.calls), 1)

    async def test_false_return_is_failure_not_private_fallback(self):
        async def send(session, chain):
            await self.send(session, chain)
            return False
        self.assertFalse((await delivery.send_jm_pdf(send=send, **self.options))[0])
        self.assertEqual(len(self.calls), 1)

    async def test_main_keeps_pdf_when_platform_returns_false(self):
        await self.check_main_delivery(outcome=False, should_keep=True)

    async def test_main_keeps_pdf_when_upload_outcome_is_unknown(self):
        await self.check_main_delivery(outcome=TimeoutError("response lost"), should_keep=True)

    async def test_main_cleans_pdf_only_after_success(self):
        await self.check_main_delivery(outcome=True, should_keep=False)

    async def test_main_respects_private_destination_whitelist(self):
        await self.check_main_delivery(outcome=Rejected(), should_keep=True, whitelist=["100"])
        self.assertTrue(all(session == self.options["umo"] for session, _ in self.calls))

    async def check_main_delivery(self, *, outcome, should_keep, whitelist=None):
        artifact = types.SimpleNamespace(pdf_path=self.pdf, pdf_size=100, title="test", cache_hit=True, password="test-only")
        async def get_or_download(album_id):
            return artifact
        async def send_message(session, chain):
            await self.send(session, chain)
            if isinstance(outcome, Exception) and isinstance(chain[0], contract.File):
                raise outcome
            return outcome if not isinstance(outcome, Exception) else True
        plugin = object.__new__(contract.Main)
        plugin._jm_download_service = types.SimpleNamespace(get_or_download=get_or_download)
        plugin._jm_max_pdf_size = 1024
        plugin._jm_cleanup_pdf_after_send = True
        plugin._policy = contract.main_module.AccessPolicy(whitelist)
        plugin.context = types.SimpleNamespace(send_message=send_message)
        await plugin._background_jm_download(umo=self.options["umo"], album_id="123456", sender_id="200")
        self.assertEqual(self.pdf.exists(), should_keep)


class ErrorDetailTests(unittest.TestCase):
    def test_error_details_omit_data_and_redact_urls_and_credentials(self):
        error = Rejected('download http://example.invalid/api/file/SECRET_TOKEN failed password="SECRET_PASSWORD"')
        error.result.update(data={"private": "SECRET_DATA"}, headers={"Authorization": "SECRET_HEADER"})
        detail = delivery.ob_error_detail(error)
        for secret in ("SECRET_TOKEN", "SECRET_PASSWORD", "SECRET_DATA", "SECRET_HEADER"):
            self.assertNotIn(secret, detail)
        self.assertIn("retcode=1200", detail)
        self.assertIn("download", detail)

    def test_plain_exception_redaction_and_bounded_single_line(self):
        detail = delivery.ob_error_detail(RuntimeError('Authorization: Bearer TOKEN_VALUE; cookie="COOKIE_VALUE";\n' + 'x' * 2000))
        self.assertNotIn("TOKEN_VALUE", detail)
        self.assertNotIn("COOKIE_VALUE", detail)
        self.assertNotIn("\n", detail)
        self.assertLessEqual(len(detail), 600)

    def test_missing_or_raising_sender_is_safe(self):
        self.assertEqual(delivery.safe_sender_id(object()), "")
        class Event:
            def get_sender_id(self):
                raise RuntimeError("unavailable")
        self.assertEqual(delivery.safe_sender_id(Event()), "")


if __name__ == "__main__":
    unittest.main()
