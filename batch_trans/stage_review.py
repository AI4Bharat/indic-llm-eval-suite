"""Stage 5 -- the human-review file.

Where ``assemble`` produces the *dataset* (one row per benchmark item, one
column per language), this stage produces the *audit trail*: one row per
translated instance, with an explicit ``language`` field and the whole history
of what happened to it -- the first translation, judge round 1, the correction,
judge round 2 -- side by side on a single line.

Input:  ``translate/``, ``judge_r*/`` and ``correct_r*/`` record files.
Output: ``review/<config>/<split>.jsonl``            every instance
        ``review/<config>/<split>.queue.jsonl``      just what a human should check
        ``review/<config>/<split>.queue.csv``        the same, as a spreadsheet
        ``review/<config>/<split>.csv``              every instance, as a spreadsheet
                                                     (only with review.csv_all_rows)
        ``review/summary.json``

Two things land in the queue: everything still scoring below the fail threshold
after the *last* judging round that saw it, and a deterministic random sample of
the rest (25% by default).  "Deterministic" matters -- the sample is a hash of
the record id, so re-running this stage hands the reviewer the same rows, and a
second reviewer can be given the same sample by name rather than by file.

Rounds are discovered from the directory names, so a re-correction pass run at a
higher bar (``correct_r2`` / ``judge_r3``, see :mod:`batch_trans.rerun`) folds in
with no extra flags.  What that pass adds is a way for the newest version of a
translation to be *worse* than an earlier one -- a corrector handed an already
good translation can still make it worse -- so each row now also carries the
score of every round (``score_history``) and of the best one (``best_score``).
Two opt-in switches act on that, both off by default:

``review.prefer_best_translation``  hand the reviewer the highest-scoring
                                    version rather than the newest one
``review.queue_regressions``        queue rows whose newest version scores below
                                    their best, even when they clear the threshold
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path

from . import paths
from .config import Config, Unit
from .jsonl import collect_jsonl, write_json, write_jsonl

# what the reviewer is being asked to look at
FAILED = "failed_final_judge"
SAMPLED = "random_sample"
UNJUDGED = "never_judged"
REGRESSED = "score_regressed"        # opt-in: newest version scores below an earlier one

_ROUND_DIR = re.compile(r"^(judge|correct)_r(\d+)$")


# --------------------------------------------------------------------------- #
# gathering
# --------------------------------------------------------------------------- #

def stage_dirs(cfg: Config, extra_inputs: list[str] | None) -> list[tuple[str, int, Path]]:
    """Every (stage, round, directory) that holds records, oldest first."""
    root = paths.benchmark_dir(cfg)
    if extra_inputs:
        candidates = [Path(p) for p in extra_inputs]
    elif root.exists():
        candidates = sorted(
            p for p in root.iterdir()
            if p.is_dir() and (p.name == "translate" or _ROUND_DIR.match(p.name))
        )
    else:
        candidates = []

    found: list[tuple[str, int, Path]] = []
    for path in candidates:
        match = _ROUND_DIR.match(path.name)
        stage, round_ = (match.group(1), int(match.group(2))) if match else ("translate", 0)
        found.append((stage, round_, path))
    # translate first, then rounds in order
    found.sort(key=lambda x: (x[1], 0 if x[0] == "correct" else 1))
    return found


def load_history(cfg: Config, extra_inputs: list[str] | None) -> dict[str, dict]:
    """Index every record by ``record_id``, keeping each stage/round separately."""
    history: dict[str, dict] = {}
    for stage, round_, path in stage_dirs(cfg, extra_inputs):
        records = collect_jsonl(path, "records.jsonl", required=False)
        failures = collect_jsonl(path, "failures.jsonl", required=False)
        label = "translate" if stage == "translate" else f"{stage}_r{round_}"
        print(f"[review] {len(records):>7} record(s), {len(failures):>4} failure(s) from {path.name}")
        for record in records:
            entry = history.setdefault(record["record_id"], {"stages": {}})
            entry["stages"][label] = record
        for failure in failures:
            key = failure.get("record_id")
            if key:
                history.setdefault(key, {"stages": {}}).setdefault("failed", []).append(label)
    if not history:
        raise FileNotFoundError(
            f"no records found under {paths.benchmark_dir(cfg)} -- run the translate stage first, "
            "or pass --input"
        )
    return history


# --------------------------------------------------------------------------- #
# per-instance assembly
# --------------------------------------------------------------------------- #

def ordered_rounds(stages: dict[str, dict], stage: str) -> list[tuple[int, dict]]:
    rounds = []
    for label, record in stages.items():
        match = _ROUND_DIR.match(label)
        if match and match.group(1) == stage:
            rounds.append((int(match.group(2)), record))
    return sorted(rounds)


def judge_view(record: dict, details: bool) -> dict:
    view = {
        "round": record.get("round"),
        "score": record.get("score"),
        "passed": record.get("passed"),
        "feedback": record.get("feedback"),
        "judged_stage": record.get("translation_stage"),
        "judged_round": record.get("translation_round"),
        "pass_threshold": record.get("pass_threshold"),
        "score_scale": record.get("score_scale"),
        "model": record.get("judge_model"),
        "prompt_path": record.get("prompt_path"),
        "timestamp": record.get("timestamp"),
        "valid": record.get("valid"),
        "validation_error": record.get("validation_error"),
    }
    if details:
        view["parsed"] = record.get("judge_parsed")
    return view


def correct_view(record: dict, details: bool) -> dict:
    view = {
        "round": record.get("round"),
        "corrected": record.get("corrected"),
        "previous_translation": record.get("previous_translation"),
        "translation": record.get("translation"),
        "judge_score_in": record.get("judge_score"),
        "judge_feedback_in": record.get("judge_feedback"),
        "source_mistake": record.get("source_mistake"),
        "model": record.get("model"),
        "prompt_path": record.get("prompt_path"),
        "timestamp": record.get("timestamp"),
        "valid": record.get("valid"),
        "validation_error": record.get("validation_error"),
    }
    if details:
        view["raw_response"] = record.get("raw_response")
    return view


def translate_view(record: dict, details: bool) -> dict:
    view = {
        "translation": record.get("translation"),
        "model": record.get("model"),
        "prompt_path": record.get("prompt_path"),
        "inference_mode": record.get("inference_mode"),
        "attempts": record.get("attempts"),
        "timestamp": record.get("timestamp"),
        "valid": record.get("valid"),
        "validation_error": record.get("validation_error"),
    }
    if details:
        view["raw_response"] = record.get("raw_response")
    return view


def best_judgement(judgements: list[tuple[int, dict]]) -> tuple[int, dict] | None:
    """The highest-scoring judgement, the later round winning a tie.

    With a single correction round "newest" and "best" are the same thing, which
    is why nothing needed this before.  A second round run at a higher bar can
    rewrite an already-good translation into a worse one, and then they differ.
    """
    scored = [(r, rec) for r, rec in judgements if rec.get("score") is not None]
    return max(scored, key=lambda pair: (pair[1]["score"], pair[0])) if scored else None


def judged_record(judgement: dict, translate: dict | None,
                  corrections: list[tuple[int, dict]]) -> dict | None:
    """The translation record a judgement was passed, by the provenance it recorded."""
    if judgement.get("translation_stage") == "correct":
        wanted = judgement.get("translation_round")
        for round_, record in corrections:
            if round_ == wanted:
                return record
        return None
    return translate


def build_row(record_id: str, entry: dict, cfg: Config) -> dict:
    """One flat row: identity, source, final translation, full history, verdict."""
    stages = entry["stages"]
    details = cfg.review.include_judge_details

    translate = stages.get("translate")
    corrections = ordered_rounds(stages, "correct")
    judgements = ordered_rounds(stages, "judge")

    # the translation a human should actually read: the newest correction, else
    # stage 1 -- or, when review.prefer_best_translation is on, whichever version
    # the judge scored highest across every round
    latest_record = corrections[-1][1] if corrections else translate
    latest_judge = judgements[-1][1] if judgements else None
    best = best_judgement(judgements)
    best_record = judged_record(best[1], translate, corrections) if best else None

    use_best = bool(cfg.review.prefer_best_translation and best_record is not None)
    final_record = best_record if use_best else latest_record
    final_judge = best[1] if use_best else latest_judge

    reference = final_record or latest_record or latest_judge
    if reference is None:
        return {}

    identity = {k: reference.get(k) for k in (
        "record_id", "benchmark", "dataset_config", "split", "row_id", "row_index",
        "language", "language_code", "group_index", "fields",
    )}
    identity["record_id"] = record_id

    source = reference.get("source") or {}
    translation = (final_record or {}).get("translation") or {}

    row = {
        **identity,
        "source": source,
        "translation": translation,
        "final_stage": (final_record or {}).get("stage"),
        "final_round": (final_record or {}).get("round"),
        "final_model": (final_record or {}).get("model"),
        "was_corrected": bool(corrections),
        "correction_rounds": len(corrections),
        "judge_rounds": [r for r, _ in judgements],
        "final_judge_round": (final_judge or {}).get("round"),
        "final_score": (final_judge or {}).get("score"),
        "final_passed": (final_judge or {}).get("passed"),
        "final_feedback": (final_judge or {}).get("feedback"),
        "first_score": judgements[0][1].get("score") if judgements else None,
        # one entry per judging round, oldest first. With two correction rounds a
        # single final_score no longer says whether quality went up or down.
        "score_history": [
            {"judge_round": r, "score": rec.get("score"), "passed": rec.get("passed"),
             "of_stage": rec.get("translation_stage"), "of_round": rec.get("translation_round")}
            for r, rec in judgements
        ],
        "best_score": best[1].get("score") if best else None,
        "best_judge_round": best[0] if best else None,
        "best_stage": (best_record or {}).get("stage") if best_record else None,
        "best_round": (best_record or {}).get("round") if best_record else None,
        "translation_picked": "best_score" if use_best else "latest_round",
        "correction_rounds_applied": [r for r, _ in corrections],
        # a correction round beyond the first, i.e. this instance went through
        # the higher-threshold re-correction pass
        "recorrected": any(r > 1 for r, _ in corrections),
        "latest_score": (latest_judge or {}).get("score"),
        # True when the newest judgement predates the newest translation, i.e. a
        # correction round ran but was never re-judged -- final_score is then stale
        "final_score_stale": bool(
            final_judge and final_record
            and (final_judge.get("translation_stage"), final_judge.get("translation_round"))
            != (final_record.get("stage"), final_record.get("round"))
        ),
        "history": {
            **({"translate": translate_view(translate, details)} if translate else {}),
            **{f"judge_r{r}": judge_view(rec, details) for r, rec in judgements},
            **{f"correct_r{r}": correct_view(rec, details) for r, rec in corrections},
        },
        "unresolved_stages": entry.get("failed") or [],
    }
    row["human_review"] = {name: "" for name in cfg.review.human_fields}
    return row


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #

def sample_fraction(seed: str, record_id: str) -> float:
    """A stable number in [0, 1) for a record -- same input, same answer, always."""
    digest = hashlib.sha256(f"{seed}|{record_id}".encode("utf-8")).hexdigest()[:8]
    return int(digest, 16) / 0x100000000


def mark_selection(rows: list[dict], cfg: Config, threshold: float) -> None:
    seed, rate = cfg.review.sample_seed, cfg.review.sample_rate
    for row in rows:
        score = row.get("final_score")
        if score is None:
            reason = UNJUDGED
        elif score < threshold:
            reason = FAILED
        elif (cfg.review.queue_regressions and row.get("best_score") is not None
              and score < row["best_score"]):
            # a later round rewrote a translation into a worse one -- worth a
            # human's eye even though it still clears the threshold
            reason = REGRESSED
        elif rate and sample_fraction(seed, row["record_id"]) < rate:
            reason = SAMPLED
        else:
            reason = ""
        row["review_reason"] = reason
        row["review_selected"] = bool(reason)


# --------------------------------------------------------------------------- #
# spreadsheet
# --------------------------------------------------------------------------- #

def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


# extra columns that only mean anything once a second correction round exists
_MULTI_ROUND_COLUMNS = ("best_score", "correction_rounds", "recorrected", "score_history")


def write_csv(path: Path, rows: list[dict], cfg: Config, multi_round: bool = False,
              judge_rounds: tuple[int, ...] = ()) -> Path:
    """A flat spreadsheet for the reviewer: source beside translation, blanks to fill.

    ``multi_round`` adds the columns describing more than one correction round,
    and ``judge_rounds`` adds one ``judge_r<N>_score`` column per round so every
    score a translation was ever given is on the line, not just the first and the
    last.  Both default off, so a benchmark that only ever ran one correction
    round gets exactly the spreadsheet it got before they existed.
    """
    field_names: list[str] = []
    for row in rows:
        for name in list(row["source"]) + list(row["translation"]):
            if name not in field_names:
                field_names.append(name)

    round_columns = [f"judge_r{n}_score" for n in judge_rounds]
    header = (
        ["record_id", "row_id", "language", "review_reason", "final_score", "was_corrected"]
        + [f"source_{n}" for n in field_names]
        + [f"translation_{n}" for n in field_names]
        + ["final_feedback", "first_score"]
        + round_columns
        + (list(_MULTI_ROUND_COLUMNS) if multi_round else [])
        + list(cfg.review.human_fields)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            flat = {
                "record_id": row["record_id"], "row_id": row["row_id"], "language": row["language"],
                "review_reason": row["review_reason"], "final_score": _cell(row["final_score"]),
                "was_corrected": row["was_corrected"], "final_feedback": _cell(row["final_feedback"]),
                "first_score": _cell(row["first_score"]),
            }
            if round_columns:
                scored = {h["judge_round"]: h["score"] for h in row.get("score_history") or []}
                flat.update({f"judge_r{n}_score": _cell(scored.get(n)) for n in judge_rounds})
            if multi_round:
                flat.update({
                    "best_score": _cell(row.get("best_score")),
                    "correction_rounds": _cell(row.get("correction_rounds_applied")),
                    "recorrected": _cell(row.get("recorrected")),
                    "score_history": _cell(
                        [[h["judge_round"], h["score"]] for h in row.get("score_history") or []]
                    ),
                })
            for name in field_names:
                flat[f"source_{name}"] = _cell(row["source"].get(name))
                flat[f"translation_{name}"] = _cell(row["translation"].get(name))
            flat.update({name: "" for name in cfg.review.human_fields})
            writer.writerow(flat)
    return path


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #

def unit_summary(rows: list[dict], queue: list[dict], threshold: float) -> dict:
    reasons: dict[str, int] = {}
    by_language: dict[str, dict[str, int]] = {}
    for row in queue:
        reasons[row["review_reason"]] = reasons.get(row["review_reason"], 0) + 1
        bucket = by_language.setdefault(row["language"], {})
        bucket[row["review_reason"]] = bucket.get(row["review_reason"], 0) + 1
    scored = [r["final_score"] for r in rows if r.get("final_score") is not None]
    best = [r["best_score"] for r in rows if r.get("best_score") is not None]
    rounds = sorted({n for r in rows for n in (r.get("correction_rounds_applied") or [])})
    return {
        "instances": len(rows),
        "queued": len(queue),
        "queued_pct": round(100 * len(queue) / len(rows), 2) if rows else 0.0,
        "by_reason": reasons,
        "by_language": by_language,
        "fail_threshold": threshold,
        "judged": len(scored),
        "mean_final_score": round(sum(scored) / len(scored), 3) if scored else None,
        "corrected": sum(1 for r in rows if r["was_corrected"]),
        # what the extra correction rounds actually bought
        "correction_rounds_used": rounds,
        "recorrected": sum(1 for r in rows if r.get("recorrected")),
        "mean_best_score": round(sum(best) / len(best), 3) if best else None,
        "below_threshold_at_best": sum(
            1 for r in rows if r.get("best_score") is not None and r["best_score"] < threshold
        ),
        "regressed": sum(
            1 for r in rows
            if r.get("best_score") is not None and r.get("final_score") is not None
            and r["final_score"] < r["best_score"]
        ),
        "stale_final_score": sum(1 for r in rows if r.get("final_score_stale")),
    }


def run(
    cfg: Config,
    inputs: list[str] | None = None,
    units: list[Unit] | None = None,
    sample_rate: float | None = None,
    fail_threshold: float | None = None,
    sample_seed: str | None = None,
    queue_only: bool = False,
    prefer_best_translation: bool | None = None,
    queue_regressions: bool | None = None,
    csv_all_rows: bool | None = None,
) -> dict:
    if sample_rate is not None:
        cfg.review.sample_rate = sample_rate
    if sample_seed is not None:
        cfg.review.sample_seed = sample_seed
    if prefer_best_translation is not None:
        cfg.review.prefer_best_translation = prefer_best_translation
    if queue_regressions is not None:
        cfg.review.queue_regressions = queue_regressions
    if csv_all_rows is not None:
        cfg.review.csv_all_rows = csv_all_rows

    judge = cfg.stages.get("judge")
    threshold = (
        fail_threshold
        if fail_threshold is not None
        else cfg.review.fail_threshold
        if cfg.review.fail_threshold is not None
        else (judge.pass_threshold if judge else 0.0)
    )

    history = load_history(cfg, inputs)
    all_rows = [row for row in (build_row(key, entry, cfg) for key, entry in history.items()) if row]
    mark_selection(all_rows, cfg, threshold)
    all_rows.sort(key=lambda r: (r["dataset_config"], r["split"], r["row_index"], r["language"]))

    # a second correction round is what makes "newest" and "best" diverge; when
    # there is only one, everything below behaves exactly as it always has
    multi_round = any(r.get("recorrected") for r in all_rows)
    # every judging round any instance passed through, so the spreadsheet can
    # carry one score column per round rather than only the first and the last
    judge_rounds = tuple(sorted(
        {h["judge_round"] for r in all_rows for h in (r.get("score_history") or [])}
    )) if multi_round else ()

    review_dir = paths.benchmark_dir(cfg) / "review"
    summary = {
        "benchmark": cfg.benchmark,
        "fail_threshold": threshold,
        "sample_rate": cfg.review.sample_rate,
        "sample_seed": cfg.review.sample_seed,
        "correction_rounds_seen": sorted(
            {n for r in all_rows for n in (r.get("correction_rounds_applied") or [])}
        ),
        "prefer_best_translation": cfg.review.prefer_best_translation,
        "queue_regressions": cfg.review.queue_regressions,
        "judge_rounds_seen": list(judge_rounds),
        "units": [],
    }

    wanted = {(u.config, u.split) for u in (units or cfg.source.units)}
    for config_name, split in sorted(wanted):
        rows = [r for r in all_rows if (r["dataset_config"], r["split"]) == (config_name, split)]
        if not rows:
            print(f"[review] {config_name}/{split}: no records, skipped")
            continue
        queue = [r for r in rows if r["review_selected"]]

        stem = review_dir / config_name / split
        written = []
        if cfg.review.include_all_rows and not queue_only:
            written.append(str(write_jsonl(f"{stem}.jsonl", rows)))
        written.append(str(write_jsonl(f"{stem}.queue.jsonl", queue)))
        if cfg.review.csv:
            if cfg.review.csv_all_rows and not queue_only:
                written.append(str(write_csv(Path(f"{stem}.csv"), rows, cfg,
                                             multi_round, judge_rounds)))
            written.append(str(write_csv(Path(f"{stem}.queue.csv"), queue, cfg,
                                         multi_round, judge_rounds)))

        entry = unit_summary(rows, queue, threshold)
        entry.update({"unit": f"{config_name}/{split}", "files": written})
        summary["units"].append(entry)
        print(
            f"[review] {config_name}/{split}: {len(rows)} instance(s), "
            f"{len(queue)} queued ({entry['queued_pct']}%) {entry['by_reason']}"
        )
        for path in written:
            print(f"[review]   -> {path}")

    write_json(review_dir / "summary.json", summary)
    return summary
