"""Stage 2 -- judging.

Input:  translation records (``records.jsonl`` from stage 1, or from a stage-3
        correction round, or any directory of such files handed over by someone
        else).
Output: ``judge_r<N>/<config>/<split>/records.jsonl`` -- one judgement per
        translation, carrying score, pass/fail, feedback and the full identity
        of the row it judged.

This stage never touches the source dataset: everything it needs is already in
the translation records, which is what makes it runnable on its own machine.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import paths
from .config import Config, Unit
from .costs import summarise
from .jsonl import collect_jsonl, write_json, write_jsonl
from .llm import LLMRequest
from .parsing import ParseError, parse_response, validate_judgement
from .prompts import load_prompt
from .records import now, prompt_values, safe_key
from .runner import run_with_retries


def unit_of(record: dict) -> Unit:
    return Unit(config=record.get("dataset_config", "default"), split=record.get("split", "train"))


def group_by_unit(records: list[dict]) -> dict[tuple[str, str], list[dict]]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for record in records:
        grouped.setdefault((record.get("dataset_config", "default"), record.get("split", "train")), []).append(record)
    return grouped


def judge_prompt_values(cfg: Config, stage, record: dict) -> dict:
    source = record["source"]
    translation = record["translation"] or {}
    values = prompt_values(cfg, record["language"], source)
    values.update(
        {f"{name}_translation": value for name, value in translation.items()},
        translation_json=json.dumps(translation, ensure_ascii=False, indent=2),
        translation_model=record.get("model", ""),
        pass_threshold=stage.pass_threshold,
        score_scale=stage.score_scale,
    )
    return values


def build_requests(cfg: Config, stage, records: list[dict], prompt) -> list[LLMRequest]:
    requests = []
    for record in records:
        requests.append(
            LLMRequest(
                key=safe_key(len(requests), record["language"], record["row_id"], f"g{record['group_index']}"),
                prompt=prompt.render(judge_prompt_values(cfg, stage, record)),
                meta={"translation_record": record, "language": record["language"]},
            )
        )
    return requests


def make_validator(output_format: str, pass_threshold: float, score_scale: float):
    def validate(request: LLMRequest, text: str):
        try:
            parsed = parse_response(text, output_format)
        except ParseError as e:
            return None, str(e)
        judgement, error = validate_judgement(parsed, pass_threshold, score_scale)
        if error:
            return None, error
        return {"judgement": judgement, "parsed": parsed}, None

    return validate


def to_record(attempt, cfg: Config, stage, prompt, round_: int) -> dict:
    source_record = attempt.request.meta["translation_record"]
    payload = attempt.payload or {}
    judgement = payload.get("judgement") or {}
    identity_keys = (
        "record_id", "benchmark", "dataset_config", "split", "row_id", "row_index",
        "language", "language_code", "group_index", "fields",
    )
    return {
        **{k: source_record.get(k) for k in identity_keys},
        "stage": "judge",
        "round": round_,
        "source": source_record.get("source"),
        "translation": source_record.get("translation"),
        # provenance of the thing being judged
        "translation_stage": source_record.get("stage"),
        "translation_round": source_record.get("round"),
        "translation_model": source_record.get("model"),
        "translation_prompt_path": source_record.get("prompt_path"),
        "translation_prompt_sha256": source_record.get("prompt_sha256"),
        # the judgement
        "score": judgement.get("score"),
        "passed": judgement.get("passed"),
        "feedback": judgement.get("feedback"),
        "pass_threshold": stage.pass_threshold,
        "score_scale": stage.score_scale,
        "judge_parsed": payload.get("parsed"),
        "judge_raw_response": attempt.result.text,
        "valid": attempt.error is None,
        "validation_error": attempt.error,
        "attempts": attempt.attempts,
        "judge_model": attempt.model,
        "judge_provider_model": attempt.provider_model,
        "judge_model_version": attempt.result.model_version,
        "inference_mode": attempt.mode,
        "prompt_path": prompt.path,
        "prompt_sha256": prompt.sha256,
        "request_key": attempt.request.key,
        "usage": attempt.result.usage,
        "timestamp": now(),
    }


def run_unit(cfg: Config, unit: Unit, records: list[dict], round_: int) -> dict:
    stage = cfg.stage("judge")
    prompt = load_prompt(stage.prompt_path)
    out_dir = paths.ensure(paths.unit_dir(cfg, "judge", unit, round_))

    usable = [r for r in records if r.get("valid") and r.get("translation")]
    skipped = len(records) - len(usable)
    if skipped:
        print(f"  [judge] skipping {skipped} record(s) with no valid translation")
    if not usable:
        print(f"[judge] {unit.label}: nothing to judge")
        return {"unit": unit.label, "stage": "judge", "judged": 0, "failed": 0,
                "total": {"requests": 0, "cost_usd": 0.0}}

    print(f"[judge] {unit.label}: {len(usable)} translations, round {round_}")
    requests = build_requests(cfg, stage, usable, prompt)
    outcome = run_with_retries(
        requests, cfg, stage, out_dir,
        validate=make_validator(stage.output_format, stage.pass_threshold, stage.score_scale),
    )

    judged = [to_record(a, cfg, stage, prompt, round_) for a in outcome.succeeded]
    failures = [to_record(a, cfg, stage, prompt, round_) for a in outcome.failed]
    write_jsonl(out_dir / "records.jsonl", judged)
    write_jsonl(out_dir / "failures.jsonl", failures)

    n_passed = sum(1 for r in judged if r["passed"])
    scores = [r["score"] for r in judged if r["score"] is not None]
    costs = summarise(outcome.usage_rows, group_keys=("language",))
    costs["by_mode"] = summarise(outcome.usage_rows, group_keys=("mode",))["by"]["groups"]
    costs.update(
        unit=unit.label, stage="judge", round=round_, model=stage.resolved_model(),
        judged=len(judged), failed=len(failures), passed=n_passed,
        failed_judgement=len(judged) - n_passed,
        mean_score=round(sum(scores) / len(scores), 3) if scores else None,
        pass_rate=round(n_passed / len(judged), 4) if judged else None,
    )
    write_json(out_dir / "costs.json", costs)

    print(
        f"[judge] {unit.label}: {n_passed}/{len(judged)} passed "
        f"(mean score {costs['mean_score']}), {len(failures)} unjudged -> {out_dir}"
    )
    return costs


def run(cfg: Config, input_path: str | None = None, round_: int = 1) -> list[dict]:
    """Judge translations found at ``input_path`` (defaults to stage 1's output)."""
    source = Path(input_path) if input_path else paths.benchmark_dir(cfg) / "translate"
    if not source.exists():
        raise FileNotFoundError(f"input not found: {source}")
    # required=False: a correction round that corrected nothing is a legitimate
    # outcome, and re-judging it is a no-op -- not a reason to abort the run.
    records = collect_jsonl(source, "records.jsonl", required=False)
    print(f"[judge] loaded {len(records)} translation record(s) from {source}")
    if not records:
        print(f"[judge] nothing to judge in {source}, round {round_} skipped")
        return []
    return [
        run_unit(cfg, Unit(config=config, split=split), unit_records, round_)
        for (config, split), unit_records in sorted(group_by_unit(records).items())
    ]
