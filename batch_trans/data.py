"""Loading source benchmarks and writing the assembled result out.

Sources are either a Hugging Face dataset (optionally with configs) or local
JSONL files.  Either way a unit becomes a plain ``list[dict]`` of rows with a
stable ``__row_id__`` attached, which is what the rest of the pipeline keys on.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from .config import Config, Unit
from .jsonl import read_jsonl, write_jsonl

ROW_ID = "__row_id__"
ROW_INDEX = "__row_index__"

# Fixed namespace, so a generated id depends only on the row -- not on when or
# where it was generated.
ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "batch-trans")


def load_unit(cfg: Config, unit: Unit, limit: int | None = None) -> list[dict]:
    """Load one (config, split) unit as a list of rows with row ids attached."""
    if cfg.source.type == "local":
        rows = read_jsonl(unit.local_path)
    else:
        import datasets

        dataset = datasets.load_dataset(
            cfg.source.path,
            name=unit.hf_config,
            split=unit.split,
            cache_dir=cfg.source.cache_dir,
            revision=cfg.source.revision,
        )
        rows = dataset.to_list()

    if limit:
        rows = rows[:limit]
    return attach_row_ids(rows, cfg.benchmark, unit, cfg.source.id_field)


def generate_row_id(benchmark: str, unit: Unit, index: int, row: dict) -> str:
    """A deterministic UUID for a row that has no id of its own.

    Derived from the row's content and position, so the same row gets the same
    id on every run and on every machine.  A random UUID would break the whole
    point of the pipeline: stage 2 run next week, or by someone else, has to be
    able to match its judgements back to stage 1's translations.
    """
    payload = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    seed = f"{benchmark}|{unit.config}|{unit.split}|{index}|{digest}"
    return str(uuid.uuid5(ID_NAMESPACE, seed))


def attach_row_ids(rows: list[dict], benchmark: str, unit: Unit, id_field: str | None) -> list[dict]:
    """Give every row a stable id, reusing ``id_field`` when the dataset has one.

    Datasets without an id column (GSM8K, for one) get a generated UUID written
    into that column, so the id travels with the data into every stage's records
    and into the final dataset.  The id identifies the *row*, not the
    translation, so all languages of a row share it -- that is what makes a row
    traceable across languages, stages and files.

    Duplicate ids in the source are disambiguated rather than silently merged --
    a collision would cross-contaminate translations between rows.

    A dataset's own id column is left exactly as it was found: MBPP's
    ``task_id`` is an int and rewriting it as a string would change the
    benchmark's schema.  Only a generated id is written into the row.
    """
    id_field = id_field or "id"
    seen: dict[str, int] = {}
    out = []
    for index, row in enumerate(rows):
        row = dict(row)
        existing = row.get(id_field)
        generated = existing in (None, "")
        row_id = generate_row_id(benchmark, unit, index, row) if generated else str(existing)
        count = seen.get(row_id, 0)
        seen[row_id] = count + 1
        if count:
            row_id = f"{row_id}#{count}"
        if generated:
            row[id_field] = row_id
        row[ROW_ID] = row_id
        row[ROW_INDEX] = index
        out.append(row)
    return out


def save_rows(path: Path, rows: list[dict]) -> Path:
    return write_jsonl(path, rows)


# --------------------------------------------------------------------------- #
# destinations
# --------------------------------------------------------------------------- #

def write_final(cfg: Config, unit: Unit, rows: list[dict], path: Path) -> Path:
    """Write the assembled parallel dataset for one unit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if cfg.output.format == "parquet":
        import datasets

        datasets.Dataset.from_list(rows).to_parquet(str(path))
    else:
        write_jsonl(path, rows)
    return path


def push_to_hub(cfg: Config, unit_rows: dict[str, dict[str, list[dict]]]) -> None:
    """Push assembled data to the Hub, one dataset config per source config.

    ``unit_rows`` is ``{config: {split: rows}}``.
    """
    import datasets

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    repo = cfg.output.hub_repo
    single_config = list(unit_rows) == ["default"]

    for config_name, splits in unit_rows.items():
        dataset_dict = datasets.DatasetDict(
            {split: datasets.Dataset.from_list(rows) for split, rows in splits.items() if rows}
        )
        if not dataset_dict:
            print(f"  [hub] skipping empty config '{config_name}'")
            continue
        kwargs: dict[str, Any] = {"repo_id": repo, "private": cfg.output.private}
        if token:
            kwargs["token"] = token
        if not single_config:
            kwargs["config_name"] = config_name
        print(f"  [hub] pushing config '{config_name}' ({', '.join(dataset_dict)}) to {repo}")
        dataset_dict.push_to_hub(**kwargs)
