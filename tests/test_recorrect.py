"""The re-correction pass, end to end with a fake model -- no API calls, no network.

Covers the thing that is easy to get wrong when a second correction round is run
at a higher bar than the first: picking the right *tail* (what the low bar walked
past, not what it already corrected), not re-judging against a stale score, and
making the review file read the two rounds in the right order.

    python -m tests.test_recorrect          (from the repo root)
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from batch_trans import paths, rerun, runner, stage_judge, stage_review, stage_translate
from batch_trans.config import load_config
from batch_trans.jsonl import read_jsonl, write_jsonl
from batch_trans.llm import LLMResult

ROWS = [
    {"id": "q1", "question": "What is 2+2?", "answer": "2+2 = <<2+2=4>>4\n#### 4"},
    {"id": "q2", "question": "What is 3*5?", "answer": "3*5 = <<3*5=15>>15\n#### 15"},
    {"id": "q3", "question": "What is 10-7?", "answer": "10-7 = <<10-7=3>>3\n#### 3"},
]

# The judge scores out of 100; round 1 corrects at 70, which is the bar we are
# about to raise to 98.  Round-1 scores below are seeded per (row, language).
ROUND1_SCORES = {
    ("q1", "Hindi"): 100.0,      # clears both bars: never touched
    ("q1", "Tamil"): 95.0,       # clears 70, misses 98: the tail we are after
    ("q2", "Hindi"): 88.0,       # ditto
    ("q2", "Tamil"): 100.0,
    ("q3", "Hindi"): 40.0,       # failed the low bar too: corrected in round 1
    ("q3", "Tamil"): 100.0,
}
ROUND2_SCORE = 60.0              # what the round-1 correction of q3/Hindi scored
ROUND3_SCORE = 99.0              # what every round-2 correction scores

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
    inference: {{mode: parallel, num_workers: 4}}
  judge:
    prompt: {prompts}/judge/math_qa.md
    model: gemini-2.5-pro
    pass_threshold: 70
    score_scale: 100
    inference: {{mode: parallel, num_workers: 4}}
  correct:
    prompt: {prompts}/correct/math_qa.md
    model: gemini-2.5-pro
    max_score: 70
    inference: {{mode: parallel, num_workers: 4}}
output:
  root: {root}
  destination: local
review:
  sample_rate: 0.0
"""

ROUND = {"judge": 1, "correct": 1}   # bumped by the driver so the fake knows which round it is


def fake_run_requests(requests, model, inference, work_dir, tag, pricing_overrides=None, vertex=None):
    """Scores round 1 from the table above, and every later round from a constant."""
    stage = ("translate" if "translate" in str(work_dir)
             else "judge" if "judge" in str(work_dir) else "correct")
    results = []
    for request in requests:
        meta = request.meta
        if stage == "translate":
            text = json.dumps({k: f"[{meta['language']}] {v}" for k, v in meta["source"].items()},
                              ensure_ascii=False)
        elif stage == "judge":
            record = meta["translation_record"]
            if ROUND["judge"] == 1:
                score = ROUND1_SCORES[(record["row_id"], record["language"])]
            elif ROUND["judge"] == 2:
                score = ROUND2_SCORE
            else:
                score = ROUND3_SCORE
            text = json.dumps({"score": score, "pass": score >= 70, "feedback": f"scored {score}"})
        else:
            record = meta["judge_record"]
            marker = "fixed" if ROUND["correct"] == 1 else "polished"
            text = json.dumps(
                {k: f"[{record['language']}][{marker}] {v}" for k, v in record["source"].items()},
                ensure_ascii=False,
            )
        results.append(LLMResult(
            key=request.key, text=text,
            usage={"input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 10,
                   "total_tokens": 160, "cost_usd": 0.001, "cost_source": "price_table"},
        ))
    return results


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        raise AssertionError(label + (f": {detail}" if detail else ""))


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="batch_trans-recorrect-"))
    repo = Path(__file__).resolve().parents[1]
    try:
        data_path = workspace / "test.jsonl"
        write_jsonl(data_path, ROWS)
        config_path = workspace / "config.yaml"
        config_path.write_text(CONFIG.format(
            data=data_path, prompts=repo / "prompts", root=workspace / "data"
        ))

        runner.run_requests = fake_run_requests
        cfg = load_config(str(config_path))
        unit = cfg.source.units[0]

        print("\n== the run as it stands: translate, judge_r1, correct_r1, judge_r2 ==")
        from batch_trans import stage_correct

        stage_translate.run(cfg)
        ROUND["judge"] = 1
        stage_judge.run(cfg, round_=1)
        stage_correct.run(cfg, round_=1)
        ROUND["judge"] = 2
        stage_judge.run(cfg, input_path=str(paths.benchmark_dir(cfg) / "correct_r1"), round_=2)

        corrected_r1 = read_jsonl(paths.unit_dir(cfg, "correct", unit, 1) / "records.jsonl")
        check("the low bar corrected only what it failed",
              [(r["row_id"], r["language"]) for r in corrected_r1] == [("q3", "Hindi")],
              str([(r["row_id"], r["language"]) for r in corrected_r1]))

        print("\n== planning the pass at the higher bar ==")
        plan = rerun.plan(cfg, threshold=98)
        picked = sorted((r["row_id"], r["language"]) for r in plan["records"])
        check("the next rounds are the free ones",
              (plan["correct_round"], plan["judge_round"]) == (2, 3),
              f"got {plan['correct_round']}, {plan['judge_round']}")
        check("exactly the tail the low bar walked past is selected",
              picked == [("q1", "Tamil"), ("q2", "Hindi")], str(picked))
        check("what round 1 already corrected is left alone",
              plan["skipped"].get(rerun.SKIP_RECORRECTED) == 1, str(plan["skipped"]))
        check("selection is judged against the newest score, not the first",
              all(r["round"] == 1 for r in plan["records"]))

        with_recorrected = rerun.plan(cfg, threshold=98, include_recorrected=True)
        check("--include-recorrected pulls in the round-1 correction too",
              sorted((r["row_id"], r["language"]) for r in with_recorrected["records"])
              == [("q1", "Tamil"), ("q2", "Hindi"), ("q3", "Hindi")])
        check("and it re-corrects from the round-2 judgement, not the round-1 one",
              next(r for r in with_recorrected["records"] if r["row_id"] == "q3")["round"] == 2)

        print("\n== a dry run touches nothing ==")
        rerun.run(cfg, threshold=98, dry_run=True)
        check("dry run writes no correction round",
              not (paths.benchmark_dir(cfg) / "correct_r2").exists())

        print("\n== running it ==")
        ROUND["judge"] = 3
        ROUND["correct"] = 2
        rerun.run(cfg, threshold=98)

        corrected_r2 = read_jsonl(paths.unit_dir(cfg, "correct", unit, 2) / "records.jsonl")
        check("both selected instances corrected", len(corrected_r2) == 2, f"got {len(corrected_r2)}")
        check("the corrector was handed the current translation",
              all(r["previous_translation"]["question"].startswith(f"[{r['language']}] ")
                  for r in corrected_r2))
        check("correction produced new text",
              all("[polished]" in r["translation"]["question"] for r in corrected_r2))

        judged_r3 = read_jsonl(paths.unit_dir(cfg, "judge", unit, 3) / "records.jsonl")
        check("the new corrections were re-judged", len(judged_r3) == 2, f"got {len(judged_r3)}")
        check("round 3 judged the round-2 correction, not the original",
              all(r["translation_stage"] == "correct" and r["translation_round"] == 2
                  for r in judged_r3))

        manifest = json.loads(
            (rerun.input_dir(cfg, 2) / "manifest.json").read_text())
        check("the manifest records the rule and the ids",
              manifest["rule"] == "score < 98" and len(manifest["record_ids"]) == 2)
        check("the manifest input directory is not mistaken for a judging round",
              not stage_review._ROUND_DIR.match(rerun.input_dir(cfg, 2).name))

        print("\n== review reads both rounds ==")
        review_dir = paths.benchmark_dir(cfg) / "review" / unit.config
        stage_review.run(cfg, fail_threshold=98)
        flat = {(r["row_id"], r["language"]): r for r in read_jsonl(review_dir / f"{unit.split}.jsonl")}

        polished = flat[("q1", "Tamil")]
        check("the whole two-round history sits on one row",
              set(polished["history"]) == {"translate", "judge_r1", "correct_r2", "judge_r3"},
              str(sorted(polished["history"])))
        check("score history is in round order",
              [(h["judge_round"], h["score"]) for h in polished["score_history"]]
              == [(1, 95.0), (3, ROUND3_SCORE)])
        check("the newest version is the one to review",
              "[polished]" in polished["translation"]["question"]
              and polished["final_score"] == ROUND3_SCORE)
        check("the row says it went through the second pass",
              polished["recorrected"] and polished["correction_rounds_applied"] == [2])
        check("a re-judged correction is not flagged stale",
              polished["final_score_stale"] is False)

        untouched = flat[("q1", "Hindi")]
        check("an instance that always cleared the bar is unchanged",
              not untouched["was_corrected"] and untouched["final_score"] == 100.0)
        check("and carries no correction rounds", untouched["correction_rounds_applied"] == [])

        regressed = flat[("q3", "Hindi")]
        check("round 1's correction scored worse than the original",
              regressed["first_score"] == 40.0 and regressed["final_score"] == ROUND2_SCORE)
        check("best_score reports the better version",
              regressed["best_score"] == ROUND2_SCORE and regressed["best_judge_round"] == 2)

        check("the re-corrected instances now clear the new bar",
              all(flat[key]["final_score"] >= 98 for key in (("q1", "Tamil"), ("q2", "Hindi"))))

        queue = read_jsonl(review_dir / f"{unit.split}.queue.jsonl")
        check("only what is still below the new bar is queued",
              sorted((r["row_id"], r["language"]) for r in queue) == [("q3", "Hindi")],
              str(sorted((r["row_id"], r["language"]) for r in queue)))
        check("queue says why", {r["review_reason"] for r in queue} == {"failed_final_judge"})

        print("\n== prefer_best_translation ==")
        # Here every newest version is also the best one, so the two modes agree;
        # what matters is that the switch is recorded and nothing silently moves.
        stage_review.run(cfg, fail_threshold=98, prefer_best_translation=True)
        flat_best = {(r["row_id"], r["language"]): r
                     for r in read_jsonl(review_dir / f"{unit.split}.jsonl")}
        check("a row whose newest version is also its best is unaffected",
              flat_best[("q1", "Tamil")]["translation"] == polished["translation"])
        check("the picker is recorded on every row",
              all(r["translation_picked"] == "best_score" for r in flat_best.values()))

        print("\nAll checks passed.\n")
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
