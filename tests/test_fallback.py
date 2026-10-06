"""Batch -> parallel fallback.

Simulates the failure mode this pipeline actually hit: a project-level throttle
that rejects a share of every batch, however many times it is resubmitted. The
fallback finishes the remainder in parallel mode instead of stranding it.

    python -m tests.test_fallback
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from batch_trans import paths, runner, stage_translate
from batch_trans.config import load_config
from batch_trans.jsonl import read_jsonl, write_jsonl
from batch_trans.llm import LLMResult

ROWS = [{"id": f"r{i}", "question": f"What is {i}+{i}?", "answer": f"#### {2 * i}"} for i in range(4)]

REJECTION = ('batch error: {"code":7,"space":"generic","message":"Request rejected due to '
             'abuse downgrade with reason: REPUTATION_TIER_LOW_REASON_COMPROMISE"}')

CONFIG = """
benchmark: fb
source:
  type: local
  files: {{test: {data}}}
fields:
  default:
    - [question, answer]
languages: [Hindi]
stages:
  translate:
    prompt: {prompts}/translate/generic.md
    model: gemini-3.7-flash
    max_attempts: 2
    inference:
      mode: batch
      temperature: 0.4
      max_output_tokens: 4096
    fallback:
      enabled: {enabled}
      max_attempts: 1
      num_workers: 7
output:
  root: {root}
  destination: local
"""

CALLS: list[dict] = []


def fake_run_requests(requests, model, inference, work_dir, tag, pricing_overrides=None, vertex=None):
    """Batch always rejected, like a reputation downgrade; parallel works."""
    CALLS.append({"tag": tag, "mode": inference.mode, "model": model,
                  "n": len(requests), "workers": inference.num_workers,
                  "temperature": inference.temperature})
    results = []
    for request in requests:
        if inference.mode == "batch":
            results.append(LLMResult(key=request.key, error=REJECTION,
                                     usage={"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
                                            "total_tokens": 0, "cost_usd": 0.0, "cost_source": "unknown"}))
        else:
            source = request.meta["source"]
            results.append(LLMResult(
                key=request.key,
                text=json.dumps({k: f"[hi] {v}" for k, v in source.items()}, ensure_ascii=False),
                usage={"input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 0,
                       "total_tokens": 150, "cost_usd": 0.002, "cost_source": "price_table"}))
    return results


def check(label, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        raise AssertionError(label + (f": {detail}" if detail else ""))


def run_case(workspace: Path, repo: Path, enabled: str):
    CALLS.clear()
    data_path = workspace / f"{enabled}.jsonl"
    write_jsonl(data_path, ROWS)
    config_path = workspace / f"config_{enabled}.yaml"
    config_path.write_text(CONFIG.format(data=data_path, prompts=repo / "prompts",
                                         root=workspace / f"data_{enabled}", enabled=enabled))
    runner.run_requests = fake_run_requests
    cfg = load_config(str(config_path))
    stage_translate.run(cfg)
    unit = cfg.source.units[0]
    out = paths.unit_dir(cfg, "translate", unit)
    return cfg, (read_jsonl(out / "records.jsonl"), read_jsonl(out / "failures.jsonl"),
                 json.loads((out / "costs.json").read_text()))


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="fallback-test-"))
    repo = Path(__file__).resolve().parents[1]
    try:
        print("\n== fallback disabled: everything strands, as it does today ==")
        cfg, (records, failures, _) = run_case(workspace, repo, "false")
        check("no successful translations", len(records) == 0)
        check("all 4 land in failures.jsonl", len(failures) == 4)
        check("the rejection reason is preserved", "REPUTATION_TIER_LOW" in failures[0]["validation_error"])
        check("only the batch attempts ran", [c["tag"] for c in CALLS] == ["attempt1", "attempt2"])

        print("\n== fallback enabled: the remainder finishes in parallel ==")
        cfg, (records, failures, costs) = run_case(workspace, repo, "true")
        check("batch attempts ran first, then the fallback",
              [c["tag"] for c in CALLS] == ["attempt1", "attempt2", "fallback1"],
              str([c["tag"] for c in CALLS]))
        check("batch phase used batch mode", {c["mode"] for c in CALLS[:2]} == {"batch"})
        check("fallback phase used parallel mode", CALLS[2]["mode"] == "parallel")
        check("fallback carried the whole remainder", CALLS[2]["n"] == 4)
        check("configured worker count reached the backend", CALLS[2]["workers"] == 7,
              f"got {CALLS[2]['workers']}")
        check("generation settings inherited from the stage", CALLS[2]["temperature"] == 0.4,
              f"got {CALLS[2]['temperature']}")
        check("bare model name provider-qualified for LiteLLM",
              CALLS[2]["model"] == "vertex_ai/gemini-3.7-flash", CALLS[2]["model"])
        check("batch phase still used the bare name", CALLS[0]["model"] == "gemini-3.7-flash")

        check("everything translated in the end", len(records) == 4 and len(failures) == 0)
        check("records name the mode that actually produced them",
              all(r["inference_mode"] == "parallel" for r in records))
        check("logical model recorded unchanged", all(r["model"] == "gemini-3.7-flash" for r in records))
        check("provider model recorded too",
              all(r["provider_model"] == "vertex_ai/gemini-3.7-flash" for r in records))
        check("attempt number continues across phases", all(r["attempts"] == 3 for r in records))

        print("\n== costs ==")
        check("every attempt counted, rejections included",
              costs["total"]["requests"] == 12, f"got {costs['total']['requests']}")
        check("cost split by mode", set(costs["by_mode"]) == {"batch", "parallel"})
        check("rejected batch requests cost nothing", costs["by_mode"]["batch"]["cost_usd"] == 0.0)
        check("the fallback is what was paid for",
              abs(costs["by_mode"]["parallel"]["cost_usd"] - 0.008) < 1e-9,
              str(costs["by_mode"]["parallel"]["cost_usd"]))

        print("\nAll checks passed.\n")
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
