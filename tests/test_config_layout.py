"""Lossless migration and runtime behavior for native configuration sections."""

import copy
import importlib
import json
import tempfile
import types
import unittest
from pathlib import Path

import test_main_contract as contract

layout = importlib.import_module(f"{contract.PACKAGE_NAME}.config_layout")
SCHEMA = json.loads(
    (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
)


def visible_spec(path):
    items = SCHEMA
    for part in path[:-1]:
        items = items[part]["items"]
    return items[path[-1]]


def defaults():
    return {
        key: copy.deepcopy(visible_spec(path)["default"])
        for key, path in layout.leaf_paths().items()
    }


class SavedConfig(dict):
    def __init__(self, values, path):
        super().__init__(copy.deepcopy(values))
        self.config_path = str(path)
        self.saved = 0

    def save_config(self):
        self.saved += 1
        Path(self.config_path).write_text(json.dumps(self), encoding="utf-8")


class LayoutTests(unittest.TestCase):
    def test_modules_have_each_option_once_and_keep_field_metadata(self):
        self.assertEqual(
            [key for key, spec in SCHEMA.items() if not spec.get("invisible")],
            [
                "general",
                "book_search",
                "image_search",
                "findchicken",
                "jm",
                "nh",
                "pdf",
            ],
        )
        self.assertEqual(len(layout.leaf_paths()), 83)
        for key, path in layout.leaf_paths().items():
            spec = visible_spec(path)
            if key == "trigger_nickname":
                self.assertEqual(path, ("general", "trigger_nickname"))
                self.assertEqual(spec["default"], "")
            legacy = SCHEMA[key]
            self.assertTrue(legacy["invisible"])
            self.assertIsNone(legacy["default"])
            for field in ("type", "min", "max", "options", "secret", "_special"):
                self.assertEqual(spec.get(field), legacy.get(field), (key, field))
        self.assertEqual(layout.leaf_paths()["google_lens_api_key"][0], "image_search")
        self.assertEqual(
            layout.leaf_paths()["jm_pdf_fixed_password"][:2], ("pdf", "jm")
        )
        self.assertEqual(
            layout.leaf_paths()["nh_pdf_fixed_password"][:2], ("pdf", "nh")
        )

    def test_all_values_round_trip_and_falsey_values_override_group_defaults(self):
        values = defaults()
        values.update(
            whitelist=[],
            cooldown_seconds=0,
            nh_download_enabled=False,
            google_lens_api_key="test-only-key",
            findchicken_llm_provider="test/model",
            jm_pdf_fixed_password="",
            findchicken_similarity_threshold=80.0,
        )
        # AstrBot fills in missing nested defaults before invoking the plugin.
        mixed = layout.grouped_config(defaults())
        mixed[layout.MIGRATED_KEY] = False
        mixed.update(values)
        original = copy.deepcopy(mixed)
        grouped = layout.grouped_config(mixed)
        self.assertEqual(layout.flatten_config(grouped), values)
        self.assertEqual(mixed, original)
        self.assertEqual(layout.grouped_config(grouped), grouped)

    def test_dashboard_alias_defaults_cannot_override_edited_nested_values(self):
        grouped = layout.grouped_config(defaults())
        # Dashboard validation replaces null aliases with type defaults.
        for key, path in layout.leaf_paths().items():
            kind = visible_spec(path)["type"]
            grouped[key] = {
                "int": 0,
                "float": 0.0,
                "bool": False,
                "string": "",
                "list": [],
            }[kind]
        grouped["image_search"]["google_lens_api_key"] = "test-only-key"
        grouped["nh"]["nh_download_enabled"] = True
        flat = layout.flatten_config(grouped)
        self.assertEqual(flat["google_lens_api_key"], "test-only-key")
        self.assertTrue(flat["nh_download_enabled"])
        self.assertNotIn(layout.MIGRATED_KEY, flat)

    def test_backup_once_save_once_and_group_edit_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            raw = json.dumps(defaults()).encode()
            path.write_bytes(raw)
            config = SavedConfig(defaults(), path)
            self.assertEqual(layout.normalize_config(config), defaults())
            self.assertEqual(config.saved, 1)
            self.assertEqual(path.with_suffix(".before-grouping.bak").read_bytes(), raw)
            config["general"]["cooldown_seconds"] = 0
            self.assertEqual(layout.normalize_config(config)["cooldown_seconds"], 0)
            self.assertEqual(config.saved, 1)

    def test_fresh_install_is_marked_before_first_dashboard_save(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            initial = layout.grouped_config(defaults())
            initial[layout.MIGRATED_KEY] = False
            path.write_text(json.dumps(initial), encoding="utf-8")
            config = SavedConfig(initial, path)
            self.assertEqual(layout.normalize_config(config), defaults())
            self.assertTrue(config[layout.MIGRATED_KEY])
            self.assertEqual(config.saved, 1)

    def test_save_failure_restores_memory_and_leaves_recovery_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            raw = json.dumps(defaults()).encode()
            path.write_bytes(raw)
            config = SavedConfig(defaults(), path)

            def fail():
                raise OSError("test save failure")

            config.save_config = fail
            with self.assertRaises(OSError):
                layout.normalize_config(config)
            self.assertEqual(config, defaults())
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(path.with_suffix(".before-grouping.bak").read_bytes(), raw)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_grouped_values_reach_existing_clients_without_behavior_changes(self):
        values = defaults()
        values.update(
            google_lens_api_key="test-only-key",
            findchicken_llm_provider="test/model",
            findchicken_similarity_threshold=80.0,
            nh_download_enabled=False,
            jm_pdf_fixed_password="test-password",
            cooldown_seconds=0,
        )
        plugin = contract.Main(types.SimpleNamespace(), layout.grouped_config(values))
        try:
            self.assertEqual(plugin.config, values)
            self.assertEqual(plugin._findchicken_llm_provider, "test/model")
            self.assertEqual(plugin._findchicken_threshold, 80.0)
            self.assertFalse(plugin._nh_download_enabled)
        finally:
            await plugin.terminate()
