"""End-to-end smoke test for arena_hard, alpaca_eval, biggenbench, frontier_math
and hle_no_tools.

These five differ from every earlier benchmark in one way that matters: all of
them read from local JSONL built by a ``scripts/prepare_*.py`` script, because
none of them can be handed to ``load_dataset`` as-is -- two need a row filter
the Hub has no config for, one is a results archive of 324 files, one is a dead
loading script, and one is a scrape.  So this asserts the prep output as much as
the pipeline: the file exists, its ids are unique, the columns the config names
are really there, and the source group the model is handed is exactly the one
field we meant to send and nothing else.

The model is faked, so this is plumbing and not translation quality: the real
configs load, the real prompts render against real rows with no unresolved
placeholder, every stage runs, and the single-field contract that
``validate_translation`` enforces holds.

Run:  python -m tests.test_added_benchmarks

Requires the prep scripts to have been run first; each benchmark is skipped with
a message naming its script if its local file is missing.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from batch_trans import (paths, runner, stage_assemble, stage_correct, stage_judge,
                         stage_review, stage_translate)
from batch_trans.config import load_config
from batch_trans.data import ROW_ID, load_unit
from batch_trans.fields import source_for_group
from batch_trans.jsonl import read_jsonl
from batch_trans.llm import LLMResult
from batch_trans.prompts import load_prompt
from batch_trans.records import prompt_values

ROWS_PER_UNIT = 6           # keep the slice small; every language still runs
LOW_SCORE_ROW = 0           # this row's judgement is forced below the 98 bar

# 97 is deliberate: below the 98 bar but above every other band, so it exercises
# `max_score: 97` exactly.
FAIL_SCORE, PASS_SCORE = 97, 100

# benchmark -> (the single field the config must send, the prep script to run)
EXPECTED = {
    "arena_hard": ("prompt", "scripts/prepare_arena_hard.py"),
    "alpaca_eval": ("instruction", "scripts/prepare_alpaca_eval.py"),
    "biggenbench": ("input", "scripts/prepare_biggenbench.py"),
    "frontier_math": ("problem", "scripts/prepare_frontier_math.py"),
    "hle_no_tools": ("question", "scripts/prepare_hle_no_tools.py"),
    "imo_answerbench": ("problem", "scripts/prepare_imo_answerbench.py"),
}


def fake_run_requests(requests, model, inference, work_dir, tag, pricing_overrides=None, vertex=None):
    stage = ("translate" if "translate" in str(work_dir)
             else "judge" if "judge" in str(work_dir) else "correct")
    results = []
    for request in requests:
        meta = request.meta
        if stage == "translate":
            text = json.dumps(_echo(meta["source"], meta["language"]), ensure_ascii=False)
        elif stage == "judge":
            record = meta["translation_record"]
            fail = record["row_index"] == LOW_SCORE_ROW
            field = record["fields"][0]
            # All five judges share one output schema: a score on 0-100, a
            # verdict, a prose summary and a list of issues.  Faking it exactly
            # is what proves validate_judgement accepts it -- including the
            # "failed but gave no feedback" guard, which a missing summary would
            # trip, and the verdict->passed mapping, which is why NEEDS_REVIEW
            # is never emitted here but PASS and FAIL are.
            text = json.dumps({
                "score": FAIL_SCORE if fail else PASS_SCORE,
                "verdict": "FAIL" if fail else "PASS",
                "needs_correction": fail,
                "source_mistake": False,
                "summary": "one awkward span" if fail else "protected content all intact",
                "issues": [{"field": field, "severity": "minor", "category": "fluency",
                            "english_span": "the", "translated_span": "the",
                            "explanation": "awkward phrasing"}] if fail else [],
            }, ensure_ascii=False)
        else:
            record = meta["judge_record"]
            fixed = _echo(record["source"], record["language"], marker="[fixed]")
            text = json.dumps({**fixed, "translation_pass": 1, "source_mistake": False},
                              ensure_ascii=False)
        results.append(LLMResult(
            key=request.key, text=text,
            usage={"input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 10,
                   "total_tokens": 160, "cost_usd": 0.001, "cost_source": "price_table"},
        ))
    return results


def _echo(source: dict, language: str, marker: str = "") -> dict:
    """A structurally valid stand-in translation: same keys, same list lengths."""
    tag = f"[{language}]{marker}"
    out = {}
    for key, value in source.items():
        if isinstance(value, list):
            out[key] = [f"{tag} {v}" for v in value]
        else:
            out[key] = f"{tag} {value}"
    return out


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        raise AssertionError(label + (f": {detail}" if detail else ""))


def check_arena_hard(rows, full):
    check("arena_hard: the sample really does carry code fences or LaTeX",
          any("```" in r["prompt"] or "\\" in r["prompt"] for r in rows))
    check("arena_hard: the English-only filter held",
          all(r.get("language") == "English" for r in full),
          str({r.get("language") for r in full}))


def check_alpaca_eval(rows, full):
    check("alpaca_eval: no row carries a separate 'input' column, as the prompts assume",
          all("input" not in r for r in full))
    check("alpaca_eval: the untranslated baseline output rides along as a source column",
          all(r.get("output") for r in full))


def check_biggenbench(rows, full):
    check("biggenbench: the Korean 'multilingual' capability is gone",
          all(r["capability"] != "multilingual" for r in full),
          str({r["capability"] for r in full}))
    check("biggenbench: system_prompt is present but never sent to the model",
          all(r.get("system_prompt") for r in full) and all(set(s) == {"input"} for s in rows))
    check("biggenbench: the rubric is flattened to six columns",
          all(f"score_rubric_{k}" in full[0] for k in
              ("criteria", "score1_description", "score2_description",
               "score3_description", "score4_description", "score5_description")),
          str(sorted(k for k in full[0] if k.startswith("score_rubric"))))


def check_frontier_math(rows, full):
    check("frontier_math: every problem carries an answer and a worked solution",
          all(r["answer"].strip() and r["solution"].strip() for r in full))
    check("frontier_math: the answer and solution are never sent to the model",
          all(set(s) == {"problem"} for s in rows))
    check("frontier_math: the scrape really did reach tier 4",
          any(r["tier"] == 4 for r in full), str(sorted({r["tier"] for r in full})))
    check("frontier_math: the sample really does carry LaTeX",
          any("\\(" in r["problem"] or "\\[" in r["problem"] for r in full))


def check_hle_no_tools(rows, full):
    check("hle_no_tools: the multi-modal rows are gone",
          all(not (r.get("image") or "") for r in full))
    check("hle_no_tools: the gold answer is never sent to the model",
          all(set(s) == {"question"} for s in rows))
    check("hle_no_tools: the split really does carry multipleChoice questions",
          any(r["answer_type"] == "multipleChoice" for r in full))
    check("hle_no_tools: multiple-choice options are inline in the question",
          all("Answer Choices" in r["question"]
              for r in full if r["answer_type"] == "multipleChoice"))
    check("hle_no_tools: the canary travels with the derived copy",
          all(r.get("canary") for r in full))


CHECKS = {"arena_hard": check_arena_hard, "alpaca_eval": check_alpaca_eval,
          "biggenbench": check_biggenbench, "frontier_math": check_frontier_math,
          "hle_no_tools": check_hle_no_tools}


def run_one(name: str, workspace: Path) -> bool:
    repo = Path(__file__).resolve().parents[1]
    cfg = load_config(str(repo / "configs" / f"{name}.yaml"))
    unit = cfg.source.units[0]
    if not Path(unit.local_path).exists():
        print(f"\n=== {name} === SKIPPED: {unit.local_path} not found, run {EXPECTED[name][1]}")
        return False

    print(f"\n=== {name} ===")
    cfg.output.root = str(workspace / name)
    for stage_name in ("translate", "judge", "correct"):
        cfg.stage(stage_name).inference.mode = "parallel"

    field = EXPECTED[name][0]
    group = cfg.groups_for(unit)
    check(f"{name}: exactly one field group holding only '{field}'",
          group == [[field]], str(group))

    full = load_unit(cfg, unit)
    rows = load_unit(cfg, unit, limit=ROWS_PER_UNIT)
    sources = [source_for_group(r, group[0]) for r in rows]
    ids = [r[ROW_ID] for r in full]
    check(f"{name}: row ids are unique across the whole split",
          len(set(ids)) == len(ids), f"{len(set(ids))} distinct of {len(ids)}")
    check(f"{name}: no id needed disambiguating with a '#' suffix",
          not any("#" in i for i in ids))
    check(f"{name}: the model is handed exactly {{'{field}'}}",
          all(set(s) == {field} for s in sources), str(sorted(sources[0])))
    check(f"{name}: no source field is empty", all(s[field].strip() for s in sources))
    CHECKS[name](sources, full)

    # every prompt renders with no unresolved placeholder, on a real row
    for stage_name in ("translate", "judge", "correct"):
        prompt = load_prompt(cfg.stage(stage_name).prompt_path)
        values = prompt_values(cfg, "Hindi", sources[0])
        if stage_name != "translate":
            record = {"source": sources[0], "translation": _echo(sources[0], "Hindi"),
                      "language": "Hindi", "model": "m", "score": 97, "passed": False,
                      "feedback": "f", "score_scale": 100, "judge_model": "m"}
            values = (stage_judge.judge_prompt_values(cfg, cfg.stage("judge"), record)
                      if stage_name == "judge"
                      else stage_correct.correction_prompt_values(cfg, record))
        missing = prompt.missing_placeholders(values)
        check(f"{name}: {stage_name} prompt has no unresolved placeholder", not missing, str(missing))

    n_expected = len(rows) * len(cfg.languages)

    stage_translate.run(cfg, limit=ROWS_PER_UNIT)
    translated = read_jsonl(paths.unit_dir(cfg, "translate", unit) / "records.jsonl")
    check(f"{name}: translated {n_expected} records", len(translated) == n_expected,
          f"got {len(translated)}")
    check(f"{name}: every translation validated",
          all(r.get("valid") for r in translated),
          str([r.get("validation_error") for r in translated if not r.get("valid")][:2]))

    stage_judge.run(cfg, round_=1)
    judged = read_jsonl(paths.unit_dir(cfg, "judge", unit, 1) / "records.jsonl")
    check(f"{name}: judged {n_expected} records", len(judged) == n_expected, f"got {len(judged)}")
    below = [r for r in judged if r["score"] < 98]
    check(f"{name}: the 97-scoring row is below the 98 bar", len(below) == len(cfg.languages),
          f"got {len(below)}")
    check(f"{name}: a FAIL verdict at 97 is read as failed",
          all(r["passed"] is False for r in below))
    check(f"{name}: a PASS verdict at 100 is read as passed",
          all(r["passed"] is True for r in judged if r["score"] == 100))

    stage_correct.run(cfg, round_=1)
    corrected = read_jsonl(paths.unit_dir(cfg, "correct", unit, 1) / "records.jsonl")
    check(f"{name}: max_score 97 selected exactly the sub-98 records",
          len(corrected) == len(below), f"corrected {len(corrected)}, expected {len(below)}")
    check(f"{name}: source_mistake is read off the corrector's response",
          all(r.get("source_mistake") is False for r in corrected))

    stage_judge.run(cfg, input_path=str(paths.benchmark_dir(cfg) / paths.stage_dirname("correct", 1)),
                    round_=2)
    stage_assemble.run(cfg)
    stage_review.run(cfg)

    summary = json.loads((paths.benchmark_dir(cfg) / "review" / "summary.json").read_text())
    total = sum(u["instances"] for u in summary["units"])
    check(f"{name}: review covers every instance", total == n_expected, f"got {total}")
    check(f"{name}: review fail_threshold is 98",
          summary["fail_threshold"] == 98, str(summary.get("fail_threshold")))
    return True


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="batch_trans-addedbench-"))
    try:
        runner.run_requests = fake_run_requests
        ran = [name for name in EXPECTED if run_one(name, workspace)]
        skipped = sorted(set(EXPECTED) - set(ran))
        print(f"\nChecked {len(ran)}/{len(EXPECTED)} benchmarks."
              + (f"  Skipped (no local data): {', '.join(skipped)}" if skipped else "  All passed."))
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
