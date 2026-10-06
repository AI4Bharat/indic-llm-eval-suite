"""Re-correction pass -- raise the bar on translations an earlier round let through.

The normal loop is ``judge_r<N>`` -> ``correct_r<N>`` -> ``judge_r<N+1>``, and
which translations a correction round picks up is decided by
``stages.correct.max_score`` *at the time it ran*.  Raising that bar afterwards
should not mean re-translating a whole benchmark: almost everything already
scores at or above the new bar, and the only work worth paying for is the tail
the old, lower bar walked past.

This module finds exactly that tail.  For every instance it takes the *newest*
judgement on disk -- ``judge_r2`` for something a correction round already
touched, ``judge_r1`` for everything else -- and keeps the ones scoring below
the new threshold.  Instances an earlier round already corrected are left alone
by default: those were not missed, they were tried and this is what the retry
produced.  Re-trying them is a separate decision (``include_recorrected``).

The selection is written out as an ordinary judge-records directory, so the
correction and re-judging that follow are the same :mod:`stage_correct` and
:mod:`stage_judge` every other round uses -- no special-cased inference path,
and the output lands in ``correct_r<N+1>`` / ``judge_r<N+2>`` where ``assemble``,
``review`` and ``costs`` already know to look.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import paths, stage_correct, stage_judge
from .config import Config, Unit
from .jsonl import collect_jsonl, write_json, write_jsonl
from .records import now

_ROUND_DIR = re.compile(r"^(judge|correct)_r(\d+)$")

# why an instance was left out of the selection
SKIP_RECORRECTED = "already_corrected"
SKIP_STALE = "correction_never_judged"


# --------------------------------------------------------------------------- #
# reading what is already on disk
# --------------------------------------------------------------------------- #

def round_dirs(cfg: Config, stage: str) -> list[tuple[int, Path]]:
    """``[(round, directory)]`` for one stage, oldest round first."""
    root = paths.benchmark_dir(cfg)
    if not root.exists():
        return []
    found = []
    for path in root.iterdir():
        match = _ROUND_DIR.match(path.name) if path.is_dir() else None
        if match and match.group(1) == stage:
            found.append((int(match.group(2)), path))
    return sorted(found)


def _index_latest(cfg: Config, stage: str) -> dict[str, tuple[int, dict]]:
    """Newest ``(round, record)`` per ``record_id`` across every round of a stage."""
    latest: dict[str, tuple[int, dict]] = {}
    for round_, path in round_dirs(cfg, stage):
        records = collect_jsonl(path, "records.jsonl", required=False)
        print(f"[recorrect] {len(records):>7} record(s) from {path.name}")
        for record in records:
            key = record.get("record_id")
            if not key:
                continue
            if key not in latest or latest[key][0] < round_:
                latest[key] = (round_, record)
    return latest


def latest_judgements(cfg: Config) -> dict[str, tuple[int, dict]]:
    return _index_latest(cfg, "judge")


def latest_corrections(cfg: Config) -> dict[str, tuple[int, dict]]:
    return _index_latest(cfg, "correct")


def next_rounds(cfg: Config) -> tuple[int, int]:
    """The next free ``(correct_round, judge_round)`` for this benchmark.

    The judge round is kept strictly above the correction round so that
    ``review`` -- which orders history by round number -- reads the new
    judgement as coming *after* the correction it judged.
    """
    correct_round = max([r for r, _ in round_dirs(cfg, "correct")] + [0]) + 1
    judge_round = max([r for r, _ in round_dirs(cfg, "judge")] + [correct_round]) + 1
    return correct_round, judge_round


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #

def select(
    judgements: dict[str, tuple[int, dict]],
    corrections: dict[str, tuple[int, dict]],
    threshold: float,
    include_recorrected: bool = False,
) -> tuple[list[dict], dict[str, list[str]]]:
    """Judge records for everything still scoring *below* ``threshold``.

    Returns the selection plus a ``{reason: [record_id]}`` map of what was
    deliberately left out, so the run log can say why the number is smaller than
    the raw count of low scores.
    """
    selected: list[dict] = []
    skipped: dict[str, list[str]] = {SKIP_RECORRECTED: [], SKIP_STALE: []}

    for key, (judge_round, record) in judgements.items():
        if not record.get("valid") or not record.get("translation"):
            continue
        score = record.get("score")
        if score is None or score >= threshold:
            continue

        correction = corrections.get(key)
        if correction is not None:
            correct_round, _ = correction
            # the judgement predates the newest correction: its score describes a
            # translation that no longer exists, so it is no basis for a rewrite
            judged_round = record.get("translation_round")
            judged_correction = record.get("translation_stage") == "correct"
            if not judged_correction or (judged_round or 0) < correct_round:
                skipped[SKIP_STALE].append(key)
                continue
            if not include_recorrected:
                skipped[SKIP_RECORRECTED].append(key)
                continue
        selected.append(record)

    selected.sort(key=lambda r: (r.get("dataset_config") or "", r.get("split") or "",
                                 r.get("row_index") or 0, r.get("language") or ""))
    return selected, {reason: ids for reason, ids in skipped.items() if ids}


def group_by_unit(records: list[dict]) -> dict[tuple[str, str], list[dict]]:
    return stage_judge.group_by_unit(records)


def input_dir(cfg: Config, correct_round: int) -> Path:
    """Where the hand-built judge-records input for this pass lives.

    Deliberately *not* named ``judge_r<N>``: it is a re-packaging of judgements
    that already exist, not a new judging round, and ``review`` must not count
    it twice.
    """
    return paths.benchmark_dir(cfg) / f"recorrect_r{correct_round}"


def write_input(cfg: Config, records: list[dict], correct_round: int) -> Path:
    """Lay the selection out as a normal stage directory ``stage_correct`` can read."""
    base = input_dir(cfg, correct_round) / "input"
    for (config_name, split), unit_records in sorted(group_by_unit(records).items()):
        write_jsonl(base / config_name / split / "records.jsonl", unit_records)
    return base


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #

def plan(cfg: Config, threshold: float, include_recorrected: bool = False,
         units: list[Unit] | None = None) -> dict:
    """Work out what a pass would do, without sending a single request."""
    judgements = latest_judgements(cfg)
    corrections = latest_corrections(cfg)
    if units is not None:
        wanted = {(u.config, u.split) for u in units}
        judgements = {
            k: v for k, v in judgements.items()
            if (v[1].get("dataset_config"), v[1].get("split")) in wanted
        }
    selected, skipped = select(judgements, corrections, threshold, include_recorrected)
    correct_round, judge_round = next_rounds(cfg)
    return {
        "benchmark": cfg.benchmark,
        "threshold": threshold,
        "rule": f"score < {threshold:g}",
        "include_recorrected": include_recorrected,
        "judged_instances": len(judgements),
        "selected": len(selected),
        "skipped": {reason: len(ids) for reason, ids in skipped.items()},
        "correct_round": correct_round,
        "judge_round": judge_round,
        "by_unit": {f"{c}/{s}": len(v) for (c, s), v in sorted(group_by_unit(selected).items())},
        "records": selected,
        "skipped_ids": skipped,
    }


def describe(plan_: dict) -> str:
    parts = [
        f"[recorrect] {plan_['benchmark']}: {plan_['selected']} of {plan_['judged_instances']} "
        f"judged instance(s) selected by [{plan_['rule']}]"
    ]
    for reason, count in sorted(plan_["skipped"].items()):
        parts.append(f"[recorrect]   skipped {count} ({reason})")
    for unit, count in plan_["by_unit"].items():
        parts.append(f"[recorrect]   {unit}: {count}")
    parts.append(
        f"[recorrect]   -> correct_r{plan_['correct_round']}, then judge_r{plan_['judge_round']}"
    )
    return "\n".join(parts)


def run(
    cfg: Config,
    threshold: float,
    include_recorrected: bool = False,
    units: list[Unit] | None = None,
    correct_round: int | None = None,
    judge_round: int | None = None,
    skip_judge: bool = False,
    dry_run: bool = False,
) -> dict:
    """Correct everything scoring below ``threshold``, then re-judge the result."""
    plan_ = plan(cfg, threshold, include_recorrected, units)
    if correct_round is not None:
        plan_["correct_round"] = correct_round
    if judge_round is not None:
        plan_["judge_round"] = judge_round
    print(describe(plan_))

    records = plan_.pop("records")
    if dry_run:
        plan_["dry_run"] = True
        return plan_

    manifest_path = input_dir(cfg, plan_["correct_round"]) / "manifest.json"
    plan_["record_ids"] = [r["record_id"] for r in records]
    plan_["timestamp"] = now()
    write_json(manifest_path, plan_)
    plan_.pop("record_ids")

    if not records:
        print(f"[recorrect] {cfg.benchmark}: nothing below {threshold:g}, nothing to do")
        plan_["correct"] = []
        plan_["judge"] = []
        return plan_

    source = write_input(cfg, records, plan_["correct_round"])
    print(f"[recorrect] selection written to {source}")

    # the selection is already exactly what should be corrected; matching the
    # stage's own cutoff to the threshold keeps it from filtering it down again
    stage = cfg.stage("correct")
    stage.max_score = threshold
    plan_["correct"] = stage_correct.run(cfg, input_path=str(source), round_=plan_["correct_round"])

    if skip_judge:
        print("[recorrect] --skip-judge: corrections written but not re-judged")
        plan_["judge"] = []
        return plan_

    corrected_dir = paths.benchmark_dir(cfg) / paths.stage_dirname("correct", plan_["correct_round"])
    plan_["judge"] = stage_judge.run(cfg, input_path=str(corrected_dir), round_=plan_["judge_round"])
    return plan_
