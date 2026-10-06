"""Minimal JSONL read/write helpers used by every stage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Iterator


def read_jsonl(path: str | Path) -> list[dict]:
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno}: invalid JSON ({e})") from e
    return rows


def iter_jsonl(path: str | Path) -> Iterator[dict]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_jsonl(path: str | Path, rows: Iterable[dict]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def append_jsonl(path: str | Path, rows: Iterable[dict]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def read_json(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str | Path, obj: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return path


def collect_jsonl(target: str | Path, filename: str, required: bool = True) -> list[dict]:
    """Read ``target`` if it is a file, else every ``filename`` under it recursively.

    Lets a stage accept either a single records file or a whole stage directory
    handed over by someone else.  With ``required=False`` an empty or missing
    target returns ``[]`` -- a correction round in which nothing needed
    correcting is a legitimate outcome, not an error.
    """
    target = Path(target)
    if target.is_file():
        return read_jsonl(target)
    if not target.exists():
        if not required:
            return []
        raise FileNotFoundError(f"input not found: {target}")
    rows: list[dict] = []
    for path in sorted(target.rglob(filename)):
        rows.extend(read_jsonl(path))
    if not rows and required:
        raise FileNotFoundError(f"no '{filename}' files with content under {target}")
    return rows
