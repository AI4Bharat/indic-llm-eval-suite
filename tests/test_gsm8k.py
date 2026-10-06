"""End-to-end test of the GSM8K prompts, schema and id generation.

Uses the real prompt files and a fake model that answers in the exact shapes
those prompts ask for -- including the judge's 0-100 score with an
``error_analysis`` list rather than a ``feedback`` string.

    python -m tests.test_gsm8k
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from batch_trans import paths, runner, stage_assemble, stage_correct, stage_judge, stage_translate
from batch_trans.config import load_config
from batch_trans.data import ROW_ID, attach_row_ids, load_unit
from batch_trans.jsonl import read_jsonl, write_jsonl
from batch_trans.llm import LLMResult
from batch_trans.prompts import load_prompt

# real GSM8K rows: `question` + newline-delimited `answer` ending in "#### n", no id column
ROWS = [
    {"question": "Janet's ducks lay 16 eggs per day. She eats three for breakfast. How many are left?",
     "answer": "Janet has 16 - 3 = <<16-3=13>>13 eggs left.\n#### 13"},
    {"question": "Tina makes $18.00 an hour. How much for 5 hours?",
     "answer": "She earns 18 * 5 = $<<18*5=90>>90.\n#### 90"},
    {"question": "A shop sells 12 apples for $3. How much do 40 apples cost?",
     "answer": "1 apple costs 3/12 = $<<3/12=0.25>>0.25.\n40 apples cost 40*0.25 = $<<40*0.25=10>>10.\n#### 10"},
]

CONFIG = """
benchmark: gsm8k_test
source:
  type: local
  files: {{test: {data}}}
fields:
  default:
    - [question, answer]
languages:
  Assamese: as
  Hindi: hi
stages:
  translate:
    prompt: {prompts}/translate/gsm8k.md
    model: gemini-2.5-flash
    max_attempts: 2
    inference: {{mode: parallel, num_workers: 4}}
  judge:
    prompt: {prompts}/judge/gsm8k.md
    model: gemini-2.5-pro
    pass_threshold: 70
    score_scale: 100
    max_attempts: 2
    inference: {{mode: parallel, num_workers: 4}}
  correct:
    prompt: {prompts}/correct/gsm8k.md
    model: gemini-2.5-pro
    max_attempts: 2
    inference: {{mode: parallel, num_workers: 4}}
output:
  root: {root}
  destination: local
  column_template: "{{field}}_{{code}}"
"""

SEEN_PROMPTS: dict[str, str] = {}


def fake_run_requests(requests, model, inference, work_dir, tag, pricing_overrides=None, vertex=None):
    """A model that answers in exactly the shape each gsm8k prompt asks for."""
    stage = ("translate" if "translate" in str(work_dir)
             else "judge" if "judge" in str(work_dir) else "correct")
    results = []
    for request in requests:
        SEEN_PROMPTS.setdefault(stage, request.prompt)
        meta = request.meta
        if stage == "translate":
            source, language = meta["source"], meta["language"]
            text = json.dumps({
                "question": f"[{language}] {source['question']}",
                # keeps the line count and copies the #### line verbatim
                "answer": "\n".join(
                    line if line.startswith("####") else f"[{language}] {line}"
                    for line in source["answer"].split("\n")
                ),
            }, ensure_ascii=False)
        elif stage == "judge":
            record = meta["translation_record"]
            bad = record["language"] == "Hindi" and record["source"]["answer"].endswith("#### 90")
            text = json.dumps({
                "analysis_cot": "Checked digits and the final line. Critical: digit altered." if bad else "Clean.",
                "error_analysis": ([{
                    "source_span": "18 * 5", "candidate_span": "18 * 6", "field": "answer",
                    "line_index": 0, "occurrence": 1, "category": "Mathematical integrity",
                    "severity": "Critical", "explanation": "digit changed",
                }] if bad else []),
                "reasoning": "A digit was altered." if bad else "Faithful and fluent.",
                "score": 20 if bad else 95,
            }, ensure_ascii=False)
        else:
            record = meta["judge_record"]
            source, language = record["source"], record["language"]
            text = json.dumps({
                "question": f"[{language}][fixed] {source['question']}",
                "answer": "\n".join(
                    line if line.startswith("####") else f"[{language}][fixed] {line}"
                    for line in source["answer"].split("\n")
                ),
                "source_mistake": False,
            }, ensure_ascii=False)
        results.append(LLMResult(
            key=request.key, text=text,
            usage={"input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 0,
                   "total_tokens": 150, "cost_usd": 0.001, "cost_source": "price_table"},
        ))
    return results


def check(label, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        raise AssertionError(label + (f": {detail}" if detail else ""))


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="gsm8k-test-"))
    repo = Path(__file__).resolve().parents[1]
    try:
        data_path = workspace / "test.jsonl"
        write_jsonl(data_path, ROWS)
        config_path = workspace / "config.yaml"
        config_path.write_text(CONFIG.format(
            data=data_path, prompts=repo / "prompts", root=workspace / "data"))

        runner.run_requests = fake_run_requests
        cfg = load_config(str(config_path))
        unit = cfg.source.units[0]

        print("\n== generated ids ==")
        rows = load_unit(cfg, unit)
        check("id column added to a dataset that had none", all("id" in r for r in rows))
        check("ids are UUIDs", all(uuid.UUID(r["id"]) for r in rows))
        check("ids are unique", len({r["id"] for r in rows}) == 3)
        check("row id mirrors the id column", all(r[ROW_ID] == r["id"] for r in rows))
        again = load_unit(cfg, unit)
        check("ids are stable across runs", [r["id"] for r in rows] == [r["id"] for r in again])
        check("ids are stable under --limit", load_unit(cfg, unit, limit=2)[1]["id"] == rows[1]["id"])
        shuffled = attach_row_ids([dict(ROWS[0])], cfg.benchmark, unit, "id")
        check("id follows the row, not just its position", shuffled[0]["id"] == rows[0]["id"])
        preset = attach_row_ids([{"id": "mine-1", "question": "q", "answer": "#### 1"}],
                                cfg.benchmark, unit, "id")
        check("an existing id is left alone", preset[0]["id"] == "mine-1")

        print("\n== stage 1: translate ==")
        stage_translate.run(cfg)
        records = read_jsonl(paths.unit_dir(cfg, "translate", unit) / "records.jsonl")
        check("one record per row x language", len(records) == 6)
        check("all translations validated", all(r["valid"] for r in records))
        check("translation keys match the gsm8k schema",
              all(set(r["translation"]) == {"question", "answer"} for r in records))
        check("answer line count preserved",
              all(len(r["translation"]["answer"].split("\n")) == len(r["source"]["answer"].split("\n"))
                  for r in records))
        check("final #### line copied verbatim",
              all(r["translation"]["answer"].split("\n")[-1] == r["source"]["answer"].split("\n")[-1]
                  for r in records))
        check("same id shared by every language of a row",
              len({r["row_id"] for r in records}) == 3)

        prompt = SEEN_PROMPTS["translate"]
        check("target_script filled in the prompt", "Bengali-Assamese" in prompt or "Devanagari" in prompt)
        check("no unfilled placeholders in the translate prompt",
              not re.search(r"\{(target_language|target_script|source_json|input_json)\}", prompt))
        check("no leftover $-placeholders", "$target" not in prompt and "$input_json" not in prompt)

        print("\n== stage 2: judge ==")
        stage_judge.run(cfg, round_=1)
        judged = read_jsonl(paths.unit_dir(cfg, "judge", unit, 1) / "records.jsonl")
        check("every translation judged", len(judged) == 6)
        check("0-100 scores accepted", {r["score"] for r in judged} == {95.0, 20.0})
        failed = [r for r in judged if not r["passed"]]
        check("score below threshold fails", len(failed) == 1 and failed[0]["score"] == 20.0)
        check("'reasoning' used as feedback", failed[0]["feedback"] == "A digit was altered.")
        check("structured findings retained",
              failed[0]["judge_parsed"]["error_analysis"][0]["severity"] == "Critical")
        check("judge prompt has no unfilled placeholders",
              not re.search(r"\{(target_language|target_script|source_json|translation_json)\}",
                            SEEN_PROMPTS["judge"]))
        check("judge prompt output block is single-braced", "{{" not in SEEN_PROMPTS["judge"])

        print("\n== stage 3: correct ==")
        stage_correct.run(cfg, round_=1)
        corrected = read_jsonl(paths.unit_dir(cfg, "correct", unit, 1) / "records.jsonl")
        check("only the failed translation corrected", len(corrected) == 1)
        check("source_mistake captured", corrected[0]["source_mistake"] is False)
        check("correction rewrote the text", "[fixed]" in corrected[0]["translation"]["question"])
        check("correction preserved the #### line",
              corrected[0]["translation"]["answer"].endswith("#### 90"))
        audit = SEEN_PROMPTS["correct"]
        check("audit findings reached the corrector", '"severity": "Critical"' in audit)
        check("correct prompt has no unfilled placeholders",
              not re.search(r"\{(target_language|target_script|source_json|translation_json|audit_json)\}", audit))

        print("\n== correction selection rules ==")
        from batch_trans.stage_correct import select_for_correction

        judged_records = [
            {"valid": True, "translation": {"q": "t"}, "passed": False, "score": 35.0},
            {"valid": True, "translation": {"q": "t"}, "passed": False, "score": 40.0},
            {"valid": True, "translation": {"q": "t"}, "passed": False, "score": 65.0},
            {"valid": True, "translation": {"q": "t"}, "passed": True,  "score": 100.0},
            {"valid": True, "translation": {"q": "t"}, "passed": False, "score": None},
            {"valid": False, "translation": None, "passed": False, "score": 10.0},
        ]
        chosen, rule = select_for_correction(judged_records)
        check("default rule follows the judge verdict", len(chosen) == 4 and rule == "judge verdict = fail",
              f"{len(chosen)} / {rule}")
        chosen, rule = select_for_correction(judged_records, max_score=40)
        check("max_score selects on the score", [r["score"] for r in chosen] == [35.0, 40.0],
              str([r["score"] for r in chosen]))
        check("max_score is inclusive", 40.0 in [r["score"] for r in chosen])
        check("rule is reported for the log", rule == "score <= 40", rule)
        check("unscored judgements never selected by score",
              all(r["score"] is not None for r in chosen))
        check("unusable records excluded either way",
              all(r["valid"] and r["translation"] for r in chosen))
        check("max_score=0 selects nothing here", select_for_correction(judged_records, 0)[0] == [])

        print("\n== stage 4: assemble ==")
        stage_assemble.run(cfg)
        final = read_jsonl(paths.final_path(cfg, unit))
        check("row count and order preserved", [r["question"] for r in final] == [r["question"] for r in ROWS])
        check("id column present in the final dataset", all(uuid.UUID(r["id"]) for r in final))
        check("id matches the one used in the records",
              {r["id"] for r in final} == {r["row_id"] for r in records})
        check("per-language columns present",
              all(c in final[0] for c in ["question_as", "answer_as", "question_hi", "answer_hi"]))
        check("correction wins for the failed row", "[fixed]" in final[1]["question_hi"])
        check("untouched language keeps its stage-1 translation", "[fixed]" not in final[1]["question_as"])

        print("\nAll checks passed.\n")
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
