"""Selecting the parts of a benchmark row that need translating.

A field selector is a column name, optionally dotted to reach inside a nested
value -- ``choices.text`` picks the ``text`` list out of ai2_arc's
``{"text": [...], "label": [...]}`` column, leaving the labels alone.

The dotted path is flattened to an underscore key (``choices_text``) for use as
a prompt placeholder, a JSON key in the model's response, and the ``{field}``
part of an output column name.
"""

from __future__ import annotations

from typing import Any


def field_key(path: str) -> str:
    """The name a field is known by in prompts, model output and output columns."""
    return path.replace(".", "_")


def get_path(row: dict, path: str) -> Any:
    value: Any = row
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(path)
        value = value[part]
    return value


def has_path(row: dict, path: str) -> bool:
    try:
        get_path(row, path)
        return True
    except KeyError:
        return False


def source_for_group(row: dict, group: list[str]) -> dict[str, Any]:
    """The ``{field_key: value}`` mapping sent to the model for one field group."""
    return {field_key(path): get_path(row, path) for path in group}


def missing_fields(row: dict, group: list[str]) -> list[str]:
    return [path for path in group if not has_path(row, path)]
