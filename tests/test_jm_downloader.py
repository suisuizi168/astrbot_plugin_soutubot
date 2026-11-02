from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from jm_downloader import (
    JmAlbumNotFoundError,
    JmDownloadService,
    JmDownloadSettings,
    JmDownloadAcquireResult,
    JmDownloadTimeoutError,
    JmPartialDownloadError,
    JmPdfGenerationError,
    normalize_jm_id,
)


class JmIdTests(unittest.TestCase):
    def test_accepts_supported_id_forms(self):
        for value in ("123456", "JM123456", "jm123456", "JM 123456"):
            self.assertEqual(normalize_jm_id(value), "123456")

    def test_extracts_explicit_jm_number_but_rejects_paths(self):
        self.assertEqual(normalize_jm_id("2026年JM123456"), "123456")
        for value in ("abc", "../../123", "../../JM123", r"..\..\JM123", "/etc", r"C:\Windows"):
            with self.assertRaises(ValueError):
                normalize_jm_id(value)
