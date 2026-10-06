"""End-to-end smoke test for the squadv2, winogrande, drop and jee_bench configs.

These four were added after the original nine, and each brings a structure the
earlier benchmarks did not have: a list field that is *empty* half the time
(squadv2's unanswerable questions), a single-underscore placeholder that has to
survive translation (winogrande), an answer list that is mostly ASCII numerals
rather than prose (drop), and a single string that carries the answer options
inline with no options column at all (jee_bench).

The model is faked, so this asserts plumbing rather than translation quality:
the real configs load, the real prompts render with no unresolved placeholder,
every stage runs, and the structural contracts that ``validate_translation``
enforces actually hold for real rows from each dataset.

Run:  python -m tests.test_new_benchmarks
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
from batch_trans.jsonl import read_jsonl
from batch_trans.llm import LLMResult
from batch_trans.prompts import load_prompt
from batch_trans.records import prompt_values
from batch_trans.fields import source_for_group
from batch_trans.data import ROW_ID, load_unit

ROWS_PER_UNIT = 6           # keep the HF slice small; every language still runs
LOW_SCORE_ROW = 0           # this row's judgement is forced below the 98 bar

# Which score the fake judge returns.  97 is deliberate: it is below the 98 bar
# but above every other band, so it exercises `max_score: 97` exactly.
FAIL_SCORE, PASS_SCORE = 97, 100


def fake_run_requests(requests, model, inference, work_dir, tag, pricing_overrides=None, vertex=None):
    stage = ("translate" if "translate" in str(work_dir)
             else "judge" if "judge" in str(work_dir) else "correct")
    results = []
    for request in requests:
        meta = request.meta
        if stage == "translate":
            source, language = meta["source"], meta["language"]
            text = json.dumps(_echo(source, language), ensure_ascii=False)
        elif stage == "judge":
            record = meta["translation_record"]
            fail = record["row_index"] == LOW_SCORE_ROW
            score = FAIL_SCORE if fail else PASS_SCORE
            if record["benchmark"] == "jee_bench":
                # jee-bench's judge has its own output schema, inherited from
                # gpqa's: a prose `summary` plus a list of `issues`, and no
                # `verdict` key, so the score alone decides pass/fail.  Faking
                # it exactly is what proves validate_judgement accepts it --
                # including the "failed but gave no feedback" guard, which a
                # missing `summary` would trip.
                payload = {
                    "score": score,
                    "source_mistake": False,
                    "summary": "labels, notation and hedging all intact"
                               if not fail else "one awkward span in the stem",
                    "issues": [] if not fail else [
                        {"field": "question", "severity": "minor", "category": "fluency",
                         "english_span": "is/are", "translated_span": "is/are",
                         "explanation": "awkward phrasing"}],
                }
            else:
                payload = {
                    "analysis_cot": "checked spans and protected content",
                    "error_analysis": [] if not fail else [{"field": "question", "severity": "Minor",
                                                            "explanation": "awkward phrasing"}],
                    "reasoning": "span alignment holds" if not fail else "one awkward span",
                    "score": score,
                }
            text = json.dumps(payload, ensure_ascii=False)
        else:
            record = meta["judge_record"]
            fixed = _echo(record["source"], record["language"], marker="[fixed]")
            text = json.dumps({**fixed, "source_mistake": False}, ensure_ascii=False)
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


# --------------------------------------------------------------------------- #
# per-benchmark structural expectations
# --------------------------------------------------------------------------- #

def check_squadv2(rows):
    empty = [r for r in rows if r["answers_text"] == []]
    check("squadv2: the split really does contain unanswerable rows",
          bool(empty), "no row with an empty answers_text in the sample")
    check("squadv2: source keys are exactly context/question/answers_text",
          set(rows[0]) == {"context", "question", "answers_text"}, str(sorted(rows[0])))


def check_winogrande(rows):
    check("winogrande: every sentence has exactly one underscore",
          all(r["sentence"].count("_") == 1 for r in rows))
    check("winogrande: source keys are exactly sentence/option1/option2",
          set(rows[0]) == {"sentence", "option1", "option2"}, str(sorted(rows[0])))


def check_drop(rows):
    numeric = [s for r in rows for s in r["answers_spans_spans"]
               if s.replace(".", "").replace("-", "").replace(",", "").isdigit()]
    check("drop: the split really does contain numeric answer spans", bool(numeric))
    check("drop: source keys are exactly passage/question/answers_spans_spans",
          set(rows[0]) == {"passage", "question", "answers_spans_spans"}, str(sorted(rows[0])))


def check_jee_bench(rows):
    check("jee_bench: source keys are exactly question",
          set(rows[0]) == {"question"}, str(sorted(rows[0])))
    check("jee_bench: the sample really does carry options inline",
          all(all(f"({letter})" in r["question"] for letter in "ABCD") for r in rows))
    check("jee_bench: the sample really does carry LaTeX",
          any("$" in r["question"] for r in rows))

    # The config deliberately does NOT use 'index' as the id_field.  If that
    # ever looks like a tidier choice, this is why it is not: 'index' is the
    # question number within a paper, so 515 rows share 54 values, and every
    # collision would be papered over with a '#n' suffix whose assignment
    # depends on row order.
    repo = Path(__file__).resolve().parents[1]
    cfg = load_config(str(repo / "configs" / "jee_bench.yaml"))
    full = load_unit(cfg, cfg.source.units[0])
    indices = [r["index"] for r in full]
    check("jee_bench: 'index' is not unique, so it cannot be the id_field",
          len(set(indices)) < len(indices), f"{len(set(indices))} distinct of {len(indices)}")
    ids = [r[ROW_ID] for r in full]
    check("jee_bench: generated ids are unique", len(set(ids)) == len(ids))
    check("jee_bench: no id needed disambiguating with a '#' suffix",
          not any("#" in i for i in ids))


CHECKS = {"squadv2": check_squadv2, "winogrande": check_winogrande,
          "drop": check_drop, "jee_bench": check_jee_bench}


def run_one(name: str, workspace: Path) -> None:
    print(f"\n=== {name} ===")
    repo = Path(__file__).resolve().parents[1]
    cfg = load_config(str(repo / "configs" / f"{name}.yaml"))
    cfg.output.root = str(workspace / name)
    for stage_name in ("translate", "judge", "correct"):
        cfg.stage(stage_name).inference.mode = "parallel"

    unit = cfg.source.units[0]
    group = cfg.groups_for(unit)[0]
    rows = load_unit(cfg, unit, limit=ROWS_PER_UNIT)
    sources = [source_for_group(r, group) for r in rows]
    row_ids = [r[ROW_ID] for r in rows]
    check(f"{name}: row ids are unique", len(set(row_ids)) == len(row_ids))
    CHECKS[name](sources)

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
          str([r.get("error") for r in translated if not r.get("valid")][:2]))
    check(f"{name}: list fields keep their length through translation",
          all(len(r["translation"][k]) == len(r["source"][k])
              for r in translated for k in r["source"] if isinstance(r["source"][k], list)))

    stage_judge.run(cfg, round_=1)
    judged = read_jsonl(paths.unit_dir(cfg, "judge", unit, 1) / "records.jsonl")
    check(f"{name}: judged {n_expected} records", len(judged) == n_expected, f"got {len(judged)}")
    below = [r for r in judged if r["score"] < 98]
    check(f"{name}: the 97-scoring row is below the 98 bar", len(below) == len(cfg.languages),
          f"got {len(below)}")
    check(f"{name}: pass_threshold 98 marks a 97 as failed",
          all(r["passed"] is False for r in below))

    stage_correct.run(cfg, round_=1)
    corrected = read_jsonl(paths.unit_dir(cfg, "correct", unit, 1) / "records.jsonl")
    check(f"{name}: max_score 97 selected exactly the sub-98 records",
          len(corrected) == len(below), f"corrected {len(corrected)}, expected {len(below)}")
    check(f"{name}: corrections keep list lengths",
          all(len(r["translation"][k]) == len(r["source"][k])
              for r in corrected for k in r["source"] if isinstance(r["source"][k], list)))

    stage_judge.run(cfg, input_path=str(paths.benchmark_dir(cfg) / paths.stage_dirname("correct", 1)),
                    round_=2)
    stage_assemble.run(cfg)
    stage_review.run(cfg)

    summary = json.loads((paths.benchmark_dir(cfg) / "review" / "summary.json").read_text())
    total = sum(u["instances"] for u in summary["units"])
    check(f"{name}: review covers every instance", total == n_expected, f"got {total}")
    check(f"{name}: review fail_threshold is 98",
          summary["fail_threshold"] == 98, str(summary.get("fail_threshold")))


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="batch_trans-newbench-"))
    try:
        runner.run_requests = fake_run_requests
        for name in ("winogrande", "squadv2", "drop", "jee_bench"):
            run_one(name, workspace)
        print("\nAll new-benchmark checks passed.")
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
