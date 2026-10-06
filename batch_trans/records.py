"""Shared record identity and prompt-variable helpers.

Every record produced by every stage carries the same identity block, so a
record from stage 3 can always be traced back to the exact benchmark row,
dataset config, split, language and field group it came from.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .config import Config, Unit

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_key(index: int, *parts: str, max_len: int = 120) -> str:
    """A request key that is unique, sortable and safe for a batch file.

    The leading ordinal guarantees uniqueness even after truncation; the rest is
    there so a raw batch output file is still readable by a human.
    """
    tail = "-".join(_UNSAFE.sub("_", str(p)) for p in parts)
    return f"{index:06d}-{tail}"[:max_len]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_id(cfg: Config, unit: Unit, row_id: str, language: str, group_index: int) -> str:
    return f"{cfg.benchmark}|{unit.config}|{unit.split}|{row_id}|{language}|g{group_index}"


def identity(
    cfg: Config,
    unit: Unit,
    row_id: str,
    row_index: int,
    language: str,
    group_index: int,
    fields: list[str],
) -> dict:
    return {
        "record_id": record_id(cfg, unit, row_id, language, group_index),
        "benchmark": cfg.benchmark,
        "dataset_config": unit.config,
        "split": unit.split,
        "row_id": row_id,
        "row_index": row_index,
        "language": language,
        "language_code": cfg.code_for(language),
        "group_index": group_index,
        "fields": list(fields),
    }


def output_skeleton(source: dict[str, Any]) -> str:
    """A JSON skeleton mirroring the source shape, for use in prompts."""
    skeleton: dict[str, Any] = {}
    for name, value in source.items():
        if isinstance(value, list):
            skeleton[name] = [f"<translated item {i + 1}>" for i in range(len(value))]
        elif isinstance(value, dict):
            skeleton[name] = {k: f"<translated {k}>" for k in value}
        else:
            skeleton[name] = f"<translated {name}>"
    return json.dumps(skeleton, ensure_ascii=False, indent=2)


def prompt_values(cfg: Config, language: str, source: dict[str, Any]) -> dict[str, Any]:
    """Variables every stage prompt can use, on top of the raw source fields."""
    return {
        **source,
        "target_language": language,
        "language": language,
        "language_code": cfg.code_for(language),
        "target_script": cfg.script_for(language),
        "source_json": json.dumps(source, ensure_ascii=False, indent=2),
        "field_names": ", ".join(source),
        "field_names_json": json.dumps(list(source), ensure_ascii=False),
        "output_schema_json": output_skeleton(source),
    }
