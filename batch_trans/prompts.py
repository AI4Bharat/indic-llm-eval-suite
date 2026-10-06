"""Prompt loading and rendering.

Prompts are plain markdown files with ``{placeholder}`` slots.  Rendering only
substitutes placeholders we actually have values for, so JSON examples inside a
prompt (``{"score": 8}``) survive untouched -- unlike ``str.format``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class Prompt:
    path: str
    text: str
    sha256: str

    def render(self, values: dict[str, Any]) -> str:
        rendered = _PLACEHOLDER.sub(
            lambda m: as_text(values[m.group(1)]) if m.group(1) in values else m.group(0),
            self.text,
        )
        return rendered

    def missing_placeholders(self, values: dict[str, Any]) -> list[str]:
        return sorted({m for m in _PLACEHOLDER.findall(self.text) if m not in values})


_cache: dict[str, Prompt] = {}


def load_prompt(path: str) -> Prompt:
    """Load (and memoise) a prompt file, recording a content hash for provenance."""
    if path in _cache:
        return _cache[path]
    text = Path(path).read_text(encoding="utf-8")
    prompt = Prompt(path=path, text=text, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])
    _cache[path] = prompt
    return prompt


def as_text(value: Any) -> str:
    """Render a field value for prompt interpolation.

    Strings pass through verbatim; lists/dicts/numbers become JSON so the model
    sees (and can mirror) an unambiguous structure.  Whole floats print as
    integers, so a threshold of 7.0 reads as "7" in the prompt.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return json.dumps(value, ensure_ascii=False)
