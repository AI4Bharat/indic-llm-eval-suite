"""Stage 4 -- assembly.

Input:  the original rows plus translation records from stage 1 and any
        correction rounds from stage 3.
Output: ``final/<config>/<split>.jsonl`` -- the original dataset with one extra
        column per (field, language), and optionally a push to the Hub.

Parallel structure is the whole point here: row *i* of the output is row *i* of
the input, with every language's translation of that row sitting on it.  A row
whose translation is missing gets ``None`` in that column and a line in
``missing.jsonl`` rather than being silently dropped or shifted.
"""

from __future__ import annotations

from pathlib import Path

from . import paths
from .config import Config, Unit
from .data import ROW_ID, ROW_INDEX, load_unit, push_to_hub, write_final
from .fields import field_key
from .jsonl import collect_jsonl, read_jsonl, write_json, write_jsonl
from .records import record_id

# later stages/rounds win over earlier ones
STAGE_RANK = {"translate": 0, "correct": 1}


def best_records(records: list[dict]) -> dict[str, dict]:
    """Pick the newest usable record per ``record_id``."""
    best: dict[str, dict] = {}
    for record in records:
        if not record.get("translation"):
            continue
        key = record["record_id"]
        rank = (STAGE_RANK.get(record.get("stage", "translate"), 0), record.get("round") or 0)
        current = best.get(key)
        if current is None or rank >= current["_rank"]:
            best[key] = {**record, "_rank": rank}
    return best


def load_records(cfg: Config, extra_inputs: list[str] | None) -> list[dict]:
    """Collect translation-shaped records from stage 1 and every correction round."""
    root = paths.benchmark_dir(cfg)
    sources: list[Path] = []
    if extra_inputs:
        sources = [Path(p) for p in extra_inputs]
    else:
        translate_dir = root / "translate"
        if translate_dir.exists():
            sources.append(translate_dir)
        sources.extend(sorted(p for p in root.glob("correct_r*") if p.is_dir()))

    records: list[dict] = []
    for source in sources:
        found = collect_jsonl(source, "records.jsonl", required=False)
        print(f"[assemble] {len(found):>7} record(s) from {source}")
        records.extend(found)
    if not records:
        raise FileNotFoundError(
            "no translation records found in " + ", ".join(str(s) for s in sources)
            + " -- run the translate stage first, or pass --input"
        )
    return records


def load_source_rows(cfg: Config, unit: Unit, limit: int | None) -> list[dict]:
    """Prefer the rows snapshot written by stage 1; fall back to re-loading the source."""
    snapshot = paths.source_dir(cfg, unit) / "rows.jsonl"
    if snapshot.exists():
        return read_jsonl(snapshot)
    print(f"[assemble] no rows snapshot for {unit.label}, re-loading source dataset")
    return load_unit(cfg, unit, limit)


def assemble_unit(cfg: Config, unit: Unit, rows: list[dict], by_id: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    groups = cfg.groups_for(unit)
    template = cfg.output.column_template
    quality = cfg.output.include_quality_columns

    out_rows, missing = [], []
    for row in rows:
        new_row = dict(row) if cfg.output.keep_source_columns else {
            ROW_ID: row[ROW_ID], ROW_INDEX: row[ROW_INDEX]
        }
        for language in cfg.languages:
            code = cfg.code_for(language)
            for group_index, group in enumerate(groups):
                key = record_id(cfg, unit, row[ROW_ID], language, group_index)
                record = by_id.get(key)
                translation = (record or {}).get("translation") or {}
                for path in group:
                    name = field_key(path)
                    column = template.format(field=name, language=language, code=code, language_code=code)
                    new_row[column] = translation.get(name)
                if record is None:
                    missing.append({
                        "record_id": key, "row_id": row[ROW_ID], "language": language,
                        "group_index": group_index, "fields": group,
                    })
                elif quality:
                    new_row[f"{language}_translation_stage"] = record.get("stage")
                    new_row[f"{language}_judge_score"] = record.get("judge_score")
        out_rows.append(new_row)
    return out_rows, missing


def run(
    cfg: Config,
    inputs: list[str] | None = None,
    destination: str | None = None,
    limit: int | None = None,
) -> dict:
    by_id = best_records(load_records(cfg, inputs))
    print(f"[assemble] {len(by_id)} unique translated (row, language, group) records")

    assembled: dict[str, dict[str, list[dict]]] = {}
    summary = {"units": [], "missing_total": 0}

    for unit in cfg.source.units:
        rows = load_source_rows(cfg, unit, limit)
        out_rows, missing = assemble_unit(cfg, unit, rows, by_id)
        path = paths.final_path(cfg, unit)
        write_final(cfg, unit, out_rows, path)
        if missing:
            write_jsonl(path.parent / f"{unit.split}.missing.jsonl", missing)
        assembled.setdefault(unit.config, {})[unit.split] = out_rows
        summary["units"].append({
            "unit": unit.label, "rows": len(out_rows), "missing": len(missing), "path": str(path),
        })
        summary["missing_total"] += len(missing)
        print(f"[assemble] {unit.label}: {len(out_rows)} rows, {len(missing)} missing translation(s) -> {path}")

    write_json(paths.benchmark_dir(cfg) / "final" / "summary.json", summary)

    destination = destination or cfg.output.destination
    if destination == "huggingface":
        push_to_hub(cfg, assembled)
    else:
        print(f"[assemble] stored locally under {paths.benchmark_dir(cfg) / 'final'}")
    return summary
