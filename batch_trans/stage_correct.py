"""Stage 3 -- correction.

Input:  judge records (``judge_r<N>/.../records.jsonl``, or any directory of
        them sent back by whoever ran stage 2).
Output: ``correct_r<N>/<config>/<split>/records.jsonl`` -- corrected
        translations written in exactly the same shape as stage 1's records.

Because the output shape matches stage 1, a correction round can be fed
straight back into stage 2 for re-judging, and stage 4 can consume translations
and corrections interchangeably.

By default only translations the judge explicitly failed are corrected; set
``max_score`` on the stage to select on the score instead.  Either way, records
the judge could not evaluate keep their original translation rather than being
rewritten on the strength of a missing verdict.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import paths
from .config import Config, Unit
from .costs import summarise
from .jsonl import collect_jsonl, write_json, write_jsonl
from .llm import LLMRequest
from .parsing import ParseError, parse_response, validate_translation
from .prompts import load_prompt
from .records import now, prompt_values, safe_key
from .runner import run_with_retries
from .stage_judge import group_by_unit


def audit_payload(record: dict) -> dict:
    """The judge's findings, as the corrector's prompt should see them.

    A judge that returns structured findings (an ``error_analysis`` list, say)
    gives the corrector far more to work with than a prose summary, so the whole
    parsed judgement is passed through when there is one.
    """
    parsed = record.get("judge_parsed")
    if isinstance(parsed, dict) and parsed:
        return parsed
    return {
        "score": record.get("score"),
        "pass": record.get("passed"),
        "reasoning": record.get("feedback") or "",
    }


def correction_prompt_values(cfg: Config, record: dict) -> dict:
    source = record["source"]
    translation = record["translation"] or {}
    audit = audit_payload(record)
    values = prompt_values(cfg, record["language"], source)
    values.update(
        {f"{name}_translation": value for name, value in translation.items()},
        previous_translation_json=json.dumps(translation, ensure_ascii=False, indent=2),
        translation_json=json.dumps(translation, ensure_ascii=False, indent=2),
        candidate_json=json.dumps(translation, ensure_ascii=False, indent=2),
        audit_json=json.dumps(audit, ensure_ascii=False, indent=2),
        error_analysis_json=json.dumps(audit.get("error_analysis", []), ensure_ascii=False, indent=2),
        judge_score=record.get("score"),
        judge_score_scale=record.get("score_scale"),
        judge_verdict="PASS" if record.get("passed") else "FAIL",
        judge_feedback=record.get("feedback") or "",
        judge_model=record.get("judge_model", ""),
    )
    return values


def build_requests(cfg: Config, records: list[dict], prompt) -> list[LLMRequest]:
    requests = []
    for record in records:
        requests.append(
            LLMRequest(
                key=safe_key(len(requests), record["language"], record["row_id"], f"g{record['group_index']}"),
                prompt=prompt.render(correction_prompt_values(cfg, record)),
                meta={"judge_record": record, "language": record["language"]},
            )
        )
    return requests


def make_validator(output_format: str):
    def validate(request: LLMRequest, text: str):
        try:
            parsed = parse_response(text, output_format)
        except ParseError as e:
            return None, str(e)
        translation, error = validate_translation(parsed, request.meta["judge_record"]["source"])
        if error:
            return None, error
        return {"translation": translation, "parsed": parsed}, None

    return validate


def to_record(attempt, cfg: Config, stage, prompt, round_: int) -> dict:
    judge_record = attempt.request.meta["judge_record"]
    payload = attempt.payload or {}
    identity_keys = (
        "record_id", "benchmark", "dataset_config", "split", "row_id", "row_index",
        "language", "language_code", "group_index", "fields",
    )
    return {
        **{k: judge_record.get(k) for k in identity_keys},
        "stage": "correct",
        "round": round_,
        "source": judge_record.get("source"),
        # a correction that failed validation keeps the previous translation, so
        # downstream stages always have something usable in "translation"
        "translation": payload.get("translation") or judge_record.get("translation"),
        "corrected": attempt.error is None,
        "previous_translation": judge_record.get("translation"),
        "previous_stage": judge_record.get("translation_stage"),
        "previous_model": judge_record.get("translation_model"),
        "judge_score": judge_record.get("score"),
        "judge_passed": judge_record.get("passed"),
        "judge_feedback": judge_record.get("feedback"),
        "judge_model": judge_record.get("judge_model"),
        # a corrector may report that the flaw was in the English source, in
        # which case an unchanged translation is the right answer
        "source_mistake": (payload.get("parsed") or {}).get("source_mistake"),
        "parsed_response": payload.get("parsed"),
        "raw_response": attempt.result.text,
        "valid": True if payload.get("translation") else bool(judge_record.get("translation")),
        "validation_error": attempt.error,
        "attempts": attempt.attempts,
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


def select_for_correction(judge_records: list[dict], max_score: float | None = None) -> tuple[list[dict], str]:
    """Which judged translations go to the corrector, and by what rule.

    By default the judge's own verdict decides.  Setting ``max_score`` switches
    to the score instead, which is what you want when the judge passes more than
    you are willing to pay to correct, or when only the clearly-broken tail is
    worth another round.  Returns the selection and a human-readable rule name
    so the run log says which one was applied.
    """
    usable = [r for r in judge_records if r.get("valid") and r.get("translation")]
    if max_score is None:
        return [r for r in usable if r.get("passed") is False], "judge verdict = fail"
    return (
        [r for r in usable if r.get("score") is not None and r["score"] <= max_score],
        f"score <= {max_score:g}",
    )


def run_unit(cfg: Config, unit: Unit, judge_records: list[dict], round_: int) -> dict:
    stage = cfg.stage("correct")
    prompt = load_prompt(stage.prompt_path)
    out_dir = paths.ensure(paths.unit_dir(cfg, "correct", unit, round_))

    needs_fix, rule = select_for_correction(judge_records, stage.max_score)
    print(
        f"[correct] {unit.label}: {len(needs_fix)} of {len(judge_records)} judged "
        f"translations selected by [{rule}] for correction (round {round_})"
    )
    if not needs_fix:
        write_jsonl(out_dir / "records.jsonl", [])
        return {"unit": unit.label, "stage": "correct", "round": round_, "corrected": 0, "failed": 0,
                "total": {"requests": 0, "cost_usd": 0.0}}

    requests = build_requests(cfg, needs_fix, prompt)
    outcome = run_with_retries(
        requests, cfg, stage, out_dir, validate=make_validator(stage.output_format)
    )

    records = [to_record(a, cfg, stage, prompt, round_) for a in outcome.succeeded]
    failures = [to_record(a, cfg, stage, prompt, round_) for a in outcome.failed]
    # failures still carry the previous translation, so keep them in records too
    write_jsonl(out_dir / "records.jsonl", records + failures)
    write_jsonl(out_dir / "failures.jsonl", failures)

    costs = summarise(outcome.usage_rows, group_keys=("language",))
    costs["by_mode"] = summarise(outcome.usage_rows, group_keys=("mode",))["by"]["groups"]
    costs.update(
        unit=unit.label, stage="correct", round=round_, model=stage.resolved_model(),
        selection_rule=rule, attempted=len(needs_fix),
        corrected=len(records), failed=len(failures),
    )
    write_json(out_dir / "costs.json", costs)

    print(f"[correct] {unit.label}: {len(records)} corrected, {len(failures)} still failing -> {out_dir}")
    return costs


def run(cfg: Config, input_path: str | None = None, round_: int = 1) -> list[dict]:
    """Correct translations the judge failed, reading judge records from ``input_path``."""
    source = Path(input_path) if input_path else paths.benchmark_dir(cfg) / paths.stage_dirname("judge", round_)
    if not source.exists():
        raise FileNotFoundError(f"input not found: {source}")
    records = collect_jsonl(source, "records.jsonl", required=False)
    print(f"[correct] loaded {len(records)} judge record(s) from {source}")
    if not records:
        print(f"[correct] nothing to correct in {source}, round {round_} skipped")
        return []
    return [
        run_unit(cfg, Unit(config=config, split=split), unit_records, round_)
        for (config, split), unit_records in sorted(group_by_unit(records).items())
    ]
