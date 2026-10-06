"""End-to-end smoke test with a fake model -- no API calls, no network.

Runs all four stages over a tiny local benchmark and checks the things that
would silently corrupt a real run: parallel structure (row i keeps its own
translations), retry of malformed output, judge routing, correction precedence,
and cost aggregation.

    python -m tests.test_pipeline          (from the repo root)
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from batch_trans import (paths, report, runner, stage_assemble, stage_correct, stage_judge,
                         stage_review, stage_translate)
from batch_trans.config import load_config
from batch_trans.jsonl import read_jsonl, write_jsonl
from batch_trans.llm import LLMRequest, LLMResult

ROWS = [
    {"id": "q1", "question": "What is 2+2?", "answer": "2+2 = <<2+2=4>>4\n#### 4"},
    {"id": "q2", "question": "What is 3*5?", "answer": "3*5 = <<3*5=15>>15\n#### 15"},
    {"id": "q3", "question": "What is 10-7?", "answer": "10-7 = <<10-7=3>>3\n#### 3"},
]

CONFIG = """
benchmark: fake_bench
source:
  type: local
  id_field: id
  files:
    test: {data}
fields:
  default:
    - [question, answer]
languages:
  Hindi: hi
  Tamil: ta
stages:
  translate:
    prompt: {prompts}/translate/math_qa.md
    model: gemini-2.5-flash
    max_attempts: 3
    inference: {{mode: parallel, num_workers: 4}}
  judge:
    prompt: {prompts}/judge/math_qa.md
    model: gemini-2.5-pro
    pass_threshold: 7
    max_attempts: 2
    inference: {{mode: parallel, num_workers: 4}}
  correct:
    prompt: {prompts}/correct/math_qa.md
    model: gemini-2.5-pro
    max_attempts: 2
    inference: {{mode: parallel, num_workers: 4}}
output:
  root: {root}
  destination: local
  column_template: "{{field}}_{{code}}"
  include_quality_columns: true
"""

# --------------------------------------------------------------------------- #
# fake model
# --------------------------------------------------------------------------- #

CALLS: dict[str, int] = {}


def fake_run_requests(requests, model, inference, work_dir, tag, pricing_overrides=None, vertex=None):
    """Deterministic stand-in for a provider.

    - translate: echoes the source with a language tag, so we can assert that
      row i really got row i's text back.
    - the Hindi translation of q2 comes back malformed on its first attempt, to
      exercise the retry path.
    - judge: fails Tamil translations of q3, passes everything else.
    - correct: returns a marked-up corrected translation.
    """
    stage = "translate" if "translate" in str(work_dir) else "judge" if "judge" in str(work_dir) else "correct"
    CALLS[stage] = CALLS.get(stage, 0) + 1
    results = []
    for request in requests:
        meta = request.meta
        if stage == "translate":
            source = meta["source"]
            language = meta["language"]
            if meta["row_id"] == "q2" and language == "Hindi" and tag == "attempt1":
                text = "sorry, I cannot do that"          # malformed -> must be retried
            else:
                text = json.dumps({k: f"[{language}] {v}" for k, v in source.items()}, ensure_ascii=False)
        elif stage == "judge":
            record = meta["translation_record"]
            fail = record["row_id"] == "q3" and record["language"] == "Tamil"
            text = json.dumps({
                "score": 3 if fail else 9,
                "pass": not fail,
                "feedback": "final answer marker dropped" if fail else "looks good",
            })
        else:
            record = meta["judge_record"]
            source = record["source"]
            text = json.dumps(
                {k: f"[{record['language']}][fixed] {v}" for k, v in source.items()}, ensure_ascii=False
            )
        results.append(LLMResult(
            key=request.key, text=text,
            usage={"input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 10,
                   "total_tokens": 160, "cost_usd": 0.001, "cost_source": "price_table"},
        ))
    return results


# --------------------------------------------------------------------------- #

def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        raise AssertionError(label + (f": {detail}" if detail else ""))


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="batch_trans-test-"))
    repo = Path(__file__).resolve().parents[1]
    try:
        data_path = workspace / "test.jsonl"
        write_jsonl(data_path, ROWS)
        config_path = workspace / "config.yaml"
        config_path.write_text(CONFIG.format(
            data=data_path, prompts=repo / "prompts", root=workspace / "data"
        ))

        runner.run_requests = fake_run_requests          # swap the model out
        cfg = load_config(str(config_path))

        print("\n== stage 1: translate ==")
        stage_translate.run(cfg)
        unit = cfg.source.units[0]
        records = read_jsonl(paths.unit_dir(cfg, "translate", unit) / "records.jsonl")
        check("one record per row x language", len(records) == 6, f"got {len(records)}")
        check("all translations valid", all(r["valid"] for r in records))
        q2_hi = next(r for r in records if r["row_id"] == "q2" and r["language"] == "Hindi")
        check("malformed response was retried", q2_hi["attempts"] == 2, f"attempts={q2_hi['attempts']}")
        check("retry produced a usable translation",
              q2_hi["translation"]["question"] == "[Hindi] What is 3*5?")
        check("translations stay attached to their own row",
              all(r["translation"]["question"] == f"[{r['language']}] {r['source']['question']}" for r in records))
        check("usage recorded per record", all(r["usage"]["input_tokens"] == 100 for r in records))

        print("\n== stage 2: judge ==")
        stage_judge.run(cfg, round_=1)
        judged = read_jsonl(paths.unit_dir(cfg, "judge", unit, 1) / "records.jsonl")
        check("every translation judged", len(judged) == 6, f"got {len(judged)}")
        failed = [r for r in judged if not r["passed"]]
        check("exactly the seeded failure failed", len(failed) == 1 and failed[0]["row_id"] == "q3")
        check("judge record traces back to the source row", failed[0]["source"]["question"] == "What is 10-7?")
        check("judge feedback retained", "marker" in failed[0]["feedback"])
        check("judge names the model that produced the translation",
              failed[0]["translation_model"] == "gemini-2.5-flash")

        print("\n== stage 3: correct ==")
        stage_correct.run(cfg, round_=1)
        corrected = read_jsonl(paths.unit_dir(cfg, "correct", unit, 1) / "records.jsonl")
        check("only failed translations corrected", len(corrected) == 1)
        check("correction carries the judge's verdict",
              corrected[0]["judge_score"] == 3 and corrected[0]["judge_passed"] is False)
        check("correction keeps the previous translation for reference",
              corrected[0]["previous_translation"]["question"] == "[Tamil] What is 10-7?")
        check("correction produced new text", "[fixed]" in corrected[0]["translation"]["question"])

        print("\n== stage 4: assemble ==")
        stage_assemble.run(cfg)
        final = read_jsonl(paths.final_path(cfg, unit))
        check("row count preserved", len(final) == 3)
        check("row order preserved", [r["id"] for r in final] == ["q1", "q2", "q3"])
        check("one column per field x language",
              all(c in final[0] for c in ["question_hi", "answer_hi", "question_ta", "answer_ta"]))
        check("source columns kept", final[0]["question"] == "What is 2+2?")
        check("each row holds its own translations",
              all(row["question_hi"] == f"[Hindi] {row['question']}" for row in final[:2]))
        check("correction wins over the original translation",
              final[2]["question_ta"] == "[Tamil][fixed] What is 10-7?")
        check("uncorrected rows keep the stage-1 translation",
              final[2]["question_hi"] == "[Hindi] What is 10-7?")
        check("quality columns present", final[2]["Tamil_translation_stage"] == "correct")
        check("no missing translations", not (paths.final_path(cfg, unit).parent / "test.missing.jsonl").exists())

        print("\n== costs ==")
        summary = report.run(cfg)
        expected_requests = 7 + 6 + 1        # translate (incl. 1 retry) + judge + correct
        check("all requests accounted for",
              summary["run_total"]["requests"] == expected_requests,
              f"got {summary['run_total']['requests']}, expected {expected_requests}")
        check("reasoning tokens tracked", summary["run_total"]["reasoning_tokens"] == expected_requests * 10)
        check("cost aggregated by stage", set(summary["by_stage"]) == {"translate", "judge_r1", "correct_r1"})
        check("cost aggregated by language", set(summary["by_language"]) == {"Hindi", "Tamil"})

        print("\n== stage 5: review ==")
        review_dir = paths.benchmark_dir(cfg) / "review" / unit.config
        stage_review.run(cfg, fail_threshold=7, sample_rate=0.0)
        flat = read_jsonl(review_dir / f"{unit.split}.jsonl")
        check("one row per instance", len(flat) == 6, f"got {len(flat)}")
        check("every row names its language",
              sorted({r["language"] for r in flat}) == ["Hindi", "Tamil"])
        check("rows ordered by source row then language",
              [(r["row_index"], r["language"]) for r in flat]
              == [(i, lang) for i in range(3) for lang in ("Hindi", "Tamil")])

        fixed = next(r for r in flat if r["row_id"] == "q3" and r["language"] == "Tamil")
        check("the whole history sits on one row",
              set(fixed["history"]) == {"translate", "judge_r1", "correct_r1"},
              str(sorted(fixed["history"])))
        check("history keeps the first translation",
              fixed["history"]["translate"]["translation"]["question"] == "[Tamil] What is 10-7?")
        check("history keeps the judge's verdict and feedback",
              fixed["history"]["judge_r1"]["score"] == 3
              and "marker" in fixed["history"]["judge_r1"]["feedback"])
        check("history keeps what the corrector was given",
              fixed["history"]["correct_r1"]["previous_translation"]["question"]
              == "[Tamil] What is 10-7?")
        check("the translation to review is the corrected one",
              "[fixed]" in fixed["translation"]["question"] and fixed["was_corrected"])
        check("a correction that was never re-judged is flagged stale",
              fixed["final_score_stale"] is True)
        check("blank columns for the reviewer", set(fixed["human_review"]) == {
            "human_verdict", "human_score", "human_issue", "human_comments", "reviewer"})

        untouched = next(r for r in flat if r["row_id"] == "q1" and r["language"] == "Hindi")
        check("a passing row carries no correction",
              not untouched["was_corrected"] and untouched["final_score"] == 9)
        check("a passing, verified row is not stale", untouched["final_score_stale"] is False)

        queue = read_jsonl(review_dir / f"{unit.split}.queue.jsonl")
        check("failures are queued for a human", len(queue) == 1 and queue[0]["record_id"] == fixed["record_id"])
        check("queue says why", queue[0]["review_reason"] == "failed_final_judge")
        check("queue spreadsheet written", (review_dir / f"{unit.split}.queue.csv").exists())

        # sampling: same seed -> same rows, and the rate is honoured
        summary_all = stage_review.run(cfg, fail_threshold=7, sample_rate=1.0)
        check("sample_rate 1.0 queues everything", summary_all["units"][0]["queued"] == 6)
        first = {r["record_id"] for r in stage_review.run(cfg, fail_threshold=7, sample_rate=0.5)
                 and read_jsonl(review_dir / f"{unit.split}.queue.jsonl")}
        second = {r["record_id"] for r in stage_review.run(cfg, fail_threshold=7, sample_rate=0.5)
                  and read_jsonl(review_dir / f"{unit.split}.queue.jsonl")}
        check("the same seed samples the same rows", first == second)
        check("sampling is a subset, not everything", 1 <= len(first) <= 6)

        print("\n== independent re-entry ==")
        # Stage 2 run against a hand-delivered directory, as a collaborator would.
        handover = workspace / "handover"
        shutil.copytree(paths.benchmark_dir(cfg) / "translate", handover)
        shutil.rmtree(paths.unit_dir(cfg, "judge", unit, 2), ignore_errors=True)
        stage_judge.run(cfg, input_path=str(handover), round_=2)
        rejudged = read_jsonl(paths.unit_dir(cfg, "judge", unit, 2) / "records.jsonl")
        check("stage 2 runs from a handed-over directory alone", len(rejudged) == 6)

        print("\n== cli wiring ==")
        from batch_trans import cli

        cli_root = workspace / "cli_data"
        cli.main([
            "run", "--config", str(config_path), "--stages", "translate,judge,correct,assemble",
            "--rounds", "1", "--mode", "parallel", "--limit", "2",
            "--output-root", str(cli_root), "--destination", "local",
        ])
        cli_final = cli_root / "fake_bench" / "final" / "default" / "test.jsonl"
        check("cli run produced a final dataset", cli_final.exists())
        cli_rows = read_jsonl(cli_final)
        check("--limit respected", len(cli_rows) == 2, f"got {len(cli_rows)}")
        check("cli output has the translation columns", "question_hi" in cli_rows[0])
        check("--output-root respected", not (workspace / "data" / "fake_bench" / "final" / "default"
                                              / "test.jsonl").samefile(cli_final))

        print("\nAll checks passed.\n")
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
