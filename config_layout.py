"""Native configuration sections with lossless legacy-flat compatibility."""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path

MIGRATED_KEY = "_config_sections_migrated"


@lru_cache(maxsize=1)
def leaf_paths() -> dict[str, tuple[str, ...]]:
    """Map visible leaf names to their native schema paths."""
    schema = json.loads(
        Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8")
    )
    paths = {}

    def visit(items, prefix=()):
        for name, spec in items.items():
            if spec.get("invisible"):
                continue
            path = (*prefix, name)
            if spec["type"] == "object":
                visit(spec["items"], path)
            else:
                if name in paths:
                    raise ValueError("Duplicate configuration leaf")
                paths[name] = path

    visit(schema)
    return paths


def flatten_config(config: dict) -> dict:
    """Read native groups and legacy flat keys without changing the input."""
    groups = {path[0] for path in leaf_paths().values()}
    flat = {
        key: value
        for key, value in config.items()
        if key not in groups
        and key != MIGRATED_KEY
        and (not config.get(MIGRATED_KEY) or key not in leaf_paths())
        and value is not None
    }
    missing = object()
    for key, path in leaf_paths().items():
        value = config
        for part in path:
            value = value.get(part, missing) if isinstance(value, dict) else missing
        if value is not missing:
            flat[key] = value
        # Legacy aliases win only on the first migration, including falsey values.
        # Dashboard validation can later replace null aliases with type defaults;
        # the persisted marker keeps those placeholders from overriding groups.
        if not config.get(MIGRATED_KEY) and key in config and config[key] is not None:
            flat[key] = config[key]
    return flat


def grouped_config(config: dict) -> dict:
    """Move existing values to sections and clear legacy aliases, idempotently."""
    result = copy.deepcopy(dict(config))
    flat = flatten_config(config)
    for key, path in leaf_paths().items():
        if key not in flat:
            continue
        node = result
        for part in path[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[path[-1]] = copy.deepcopy(flat[key])
        result[key] = None
    result[MIGRATED_KEY] = True
    return result


def normalize_config(config: dict) -> dict:
    """Persist one legacy migration, then return the existing flat runtime view."""
    flat = flatten_config(config)
    save = getattr(config, "save_config", None)
    if not config.get(MIGRATED_KEY) and callable(save):
        path = getattr(config, "config_path", None)
        if path:
            path = Path(path)
            backup = path.with_suffix(".before-grouping.bak")
            original_bytes = path.read_bytes()
            try:
                with backup.open("xb") as stream:
                    stream.write(original_bytes)
            except FileExistsError:
                pass
        original = copy.deepcopy(dict(config))
        config.clear()
        config.update(grouped_config(original))
        try:
            save()
        except Exception:
            config.clear()
            config.update(original)
            raise
    return flat
