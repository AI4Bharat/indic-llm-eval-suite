"""Stage 1 -- translation.

Input:  the source benchmark (Hugging Face or local JSONL).
Output: ``translate/<config>/<split>/records.jsonl`` -- one validated
        translation per (row, language, field group), plus the full request /
        result audit trail and a cost summary.

One request is issued per (row, language, field group).  Grouping fields keeps
mutually dependent text together (a question and its options are translated in
one call so they stay consistent), while keeping languages separate keeps each
response small and cheap to retry.
"""

from __future__ import annotations

from pathlib import Path

from . import paths
from .config import Config, Unit
from .costs import summarise
from .data import ROW_ID, ROW_INDEX, load_unit, save_rows
from .fields import missing_fields, source_for_group
from .jsonl import write_json, write_jsonl
from .llm import LLMRequest
from .parsing import ParseError, parse_response, validate_translation
from .prompts import load_prompt
from .records import identity, now, prompt_values, safe_key
from .runner import run_with_retries


def build_requests(cfg: Config, unit: Unit, rows: list[dict], prompt) -> list[LLMRequest]:
    groups = cfg.groups_for(unit)
    if rows:
        missing = sorted({f for group in groups for f in missing_fields(rows[0], group)})
        if missing:
            raise ValueError(
                f"config: fields {missing} are not present in {unit.label} "
                f"(available columns: {sorted(k for k in rows[0] if not k.startswith('__'))})"
            )

    requests: list[LLMRequest] = []
    for row in rows:
        for group_index, group in enumerate(groups):
            source = source_for_group(row, group)
            for language in cfg.languages:
                meta = {
                    **identity(cfg, unit, row[ROW_ID], row[ROW_INDEX], language, group_index, group),
                    "source": source,
                }
                requests.append(
                    LLMRequest(
                        key=safe_key(len(requests), language, row[ROW_ID], f"g{group_index}"),
                        prompt=prompt.render(prompt_values(cfg, language, source)),
                        meta=meta,
                    )
                )
    return requests


def make_validator(output_format: str):
    def validate(request: LLMRequest, text: str):
        try:
            parsed = parse_response(text, output_format)
        except ParseError as e:
            return None, str(e)
        translation, error = validate_translation(parsed, request.meta["source"])
        if error:
            return None, error
        return {"translation": translation, "parsed": parsed}, None

    return validate


def to_record(attempt, cfg: Config, stage, prompt) -> dict:
    meta = attempt.request.meta
    payload = attempt.payload or {}
    return {
        **meta,
        "stage": "translate",
        "round": 0,
        "translation": payload.get("translation"),
        "parsed_response": payload.get("parsed"),
        "raw_response": attempt.result.text,
        "valid": attempt.error is None,
        "validation_error": attempt.error,
        "attempts": attempt.attempts,
        # the mode/model that actually produced this record, which is not the
        # stage default when a fallback phase ran
        "model": attempt.model,
        "provider_model": attempt.provider_model,
        "inference_mode": attempt.mode,
        "model_version": attempt.result.model_version,
        "prompt_path": prompt.path,
        "prompt_sha256": prompt.sha256,
        "request_key": attempt.request.key,
        "usage": attempt.result.usage,
        "timestamp": now(),
    }


def run_unit(cfg: Config, unit: Unit, limit: int | None = None) -> dict:
    stage = cfg.stage("translate")
    prompt = load_prompt(stage.prompt_path)
    out_dir = paths.ensure(paths.unit_dir(cfg, "translate", unit))

    rows = load_unit(cfg, unit, limit)
    save_rows(paths.source_dir(cfg, unit) / "rows.jsonl", rows)
    print(f"[translate] {unit.label}: {len(rows)} rows x {len(cfg.languages)} languages")

    requests = build_requests(cfg, unit, rows, prompt)
    if not requests:
        print(f"[translate] {unit.label}: nothing to translate")
        return {"unit": unit.label, "stage": "translate", "translated": 0, "failed": 0,
                "total": {"requests": 0, "cost_usd": 0.0}}

    unresolved = prompt.missing_placeholders(
        prompt_values(cfg, cfg.languages[0], requests[0].meta["source"])
    )
    if unresolved:
        print(f"  [translate] note: prompt placeholders left unfilled: {unresolved}")

    outcome = run_with_retries(
        requests, cfg, stage, out_dir, validate=make_validator(stage.output_format)
    )

    records = [to_record(a, cfg, stage, prompt) for a in outcome.succeeded]
    failures = [to_record(a, cfg, stage, prompt) for a in outcome.failed]
    write_jsonl(out_dir / "records.jsonl", records)
    write_jsonl(out_dir / "failures.jsonl", failures)

    costs = summarise(outcome.usage_rows, group_keys=("language",))
    costs["by_mode"] = summarise(outcome.usage_rows, group_keys=("mode",))["by"]["groups"]
    costs["unit"] = unit.label
    costs["stage"] = "translate"
    costs["model"] = stage.resolved_model()
    costs["translated"] = len(records)
    costs["failed"] = len(failures)
    write_json(out_dir / "costs.json", costs)

    print(f"[translate] {unit.label}: {len(records)} ok, {len(failures)} failed -> {out_dir}")
    return costs


def run(cfg: Config, limit: int | None = None, units: list[Unit] | None = None) -> list[dict]:
    return [run_unit(cfg, unit, limit) for unit in (units or cfg.source.units)]
