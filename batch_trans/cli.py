"""Command line entry point.

Every stage is a separate subcommand that reads files and writes files, so any
stage can be run on its own machine, in its own process, days apart::

    python -m batch_trans.cli translate --config configs/gsm8k.yaml
    python -m batch_trans.cli judge     --config configs/gsm8k.yaml --input handed_over/translate
    python -m batch_trans.cli correct   --config configs/gsm8k.yaml --input handed_over/judge_r1
    python -m batch_trans.cli assemble  --config configs/gsm8k.yaml --destination huggingface
    python -m batch_trans.cli recorrect --config configs/gsm8k.yaml --threshold 98
    python -m batch_trans.cli review    --config configs/gsm8k.yaml
    python -m batch_trans.cli costs     --config configs/gsm8k.yaml

``run`` chains stages in one process, purely as a convenience -- it uses the
same file interfaces as running them separately.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:                                   # optional; keeps credentials out of the shell history
    from dotenv import find_dotenv, load_dotenv

    # usecwd: find .env from the working directory, not from this file's location,
    # so `python -m batch_trans.cli` works the same from anywhere
    load_dotenv(find_dotenv(usecwd=True) or ".env")
except ImportError:
    pass

from . import (
    paths, report, rerun, stage_assemble, stage_correct, stage_judge, stage_review, stage_translate,
)
from .config import load_config
from .fields import source_for_group
from .prompts import load_prompt
from .records import prompt_values


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True, help="path to the benchmark YAML config")
    parser.add_argument("--mode", choices=["batch", "parallel"],
                        help="override inference.mode for the stage(s) being run")
    parser.add_argument("--model", help="override the stage model")
    parser.add_argument("--output-root", help="override output.root")


def _apply_overrides(cfg, args, stage_names: list[str]) -> None:
    if args.output_root:
        cfg.output.root = args.output_root
    for name in stage_names:
        stage = cfg.stages.get(name)
        if stage is None:
            continue
        if args.mode:
            stage.inference.mode = args.mode
        if getattr(args, "model", None):
            stage.model = args.model
            stage.inference.litellm_model = None
    # fail before loading a dataset if batch mode has nowhere to put its files
    if any(cfg.stages[n].inference.mode == "batch" for n in stage_names if n in cfg.stages):
        cfg.vertex.require("batch mode")
        print(f"[vertex] {cfg.vertex.describe()}")


def _select_units(cfg, args):
    units = cfg.source.units
    if getattr(args, "split", None):
        units = [u for u in units if u.split in args.split]
    if getattr(args, "dataset_config", None):
        units = [u for u in units if u.config in args.dataset_config]
    if not units:
        raise SystemExit("no (config, split) units left after filtering")
    return units


def cmd_translate(args) -> None:
    cfg = load_config(args.config)
    _apply_overrides(cfg, args, ["translate"])
    stage_translate.run(cfg, limit=args.limit, units=_select_units(cfg, args))
    report.run(cfg)


def cmd_judge(args) -> None:
    cfg = load_config(args.config)
    _apply_overrides(cfg, args, ["judge"])
    stage_judge.run(cfg, input_path=args.input, round_=args.round)
    report.run(cfg)


def cmd_correct(args) -> None:
    cfg = load_config(args.config)
    _apply_overrides(cfg, args, ["correct"])
    stage_correct.run(cfg, input_path=args.input, round_=args.round)
    report.run(cfg)


def cmd_recorrect(args) -> None:
    cfg = load_config(args.config)
    _apply_overrides(cfg, args, [] if args.dry_run else ["correct", "judge"])
    rerun.run(
        cfg,
        threshold=args.threshold,
        include_recorrected=args.include_recorrected,
        units=_select_units(cfg, args),
        correct_round=args.correct_round,
        judge_round=args.judge_round,
        skip_judge=args.skip_judge,
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        report.run(cfg)


def cmd_assemble(args) -> None:
    cfg = load_config(args.config)
    _apply_overrides(cfg, args, [])
    stage_assemble.run(cfg, inputs=args.input, destination=args.destination, limit=args.limit)


def cmd_review(args) -> None:
    cfg = load_config(args.config)
    if args.output_root:
        cfg.output.root = args.output_root
    stage_review.run(
        cfg,
        inputs=args.input,
        units=_select_units(cfg, args),
        sample_rate=args.sample_rate,
        fail_threshold=args.fail_threshold,
        sample_seed=args.seed,
        queue_only=args.queue_only,
        prefer_best_translation=True if args.prefer_best_translation else None,
        queue_regressions=True if args.queue_regressions else None,
        csv_all_rows=True if args.csv_all else None,
    )


def cmd_costs(args) -> None:
    mode = "force" if args.reprice else "off" if args.no_reprice else "gaps"
    report.run(load_config(args.config), reprice_mode=mode)


def cmd_run(args) -> None:
    cfg = load_config(args.config)
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    _apply_overrides(cfg, args, stages)
    units = _select_units(cfg, args)

    if "translate" in stages:
        stage_translate.run(cfg, limit=args.limit, units=units)
    for round_ in range(1, args.rounds + 1):
        if "judge" in stages:
            judge_input = None if round_ == 1 else str(
                paths.benchmark_dir(cfg) / paths.stage_dirname("correct", round_ - 1)
            )
            stage_judge.run(cfg, input_path=judge_input, round_=round_)
        if "correct" in stages:
            stage_correct.run(cfg, round_=round_)
    if "assemble" in stages:
        stage_assemble.run(cfg, destination=args.destination, limit=args.limit)
    if "review" in stages:
        stage_review.run(cfg, units=units)
    report.run(cfg)


def cmd_preview(args) -> None:
    """Render one prompt per stage against the first row -- no API calls."""
    cfg = load_config(args.config)
    unit = _select_units(cfg, args)[0]
    from .data import load_unit

    rows = load_unit(cfg, unit, limit=1)
    if not rows:
        raise SystemExit(f"{unit.label} is empty")
    group = cfg.groups_for(unit)[0]
    source = source_for_group(rows[0], group)
    language = cfg.languages[0]

    stage = cfg.stage(args.stage)
    prompt = load_prompt(stage.prompt_path)
    values = prompt_values(cfg, language, source)
    if args.stage in ("judge", "correct"):
        judge = cfg.stages.get("judge")
        fake = {name: f"<{language} translation of {name}>" for name in source}
        values.update({f"{k}_translation": v for k, v in fake.items()})
        threshold = judge.pass_threshold if judge else stage.pass_threshold
        scale = judge.score_scale if judge else stage.score_scale
        candidate = json.dumps(fake, ensure_ascii=False, indent=2)
        audit = {
            "score": round(threshold / 2),
            "pass": False,
            "reasoning": "<judge reasoning>",
            "error_analysis": [{"source_span": "<english span>", "candidate_span": "<target span>",
                                "severity": "Major", "explanation": "<why>"}],
        }
        values.update(
            translation_json=candidate, previous_translation_json=candidate, candidate_json=candidate,
            translation_model="<translation model>",
            audit_json=json.dumps(audit, ensure_ascii=False, indent=2),
            error_analysis_json=json.dumps(audit["error_analysis"], ensure_ascii=False, indent=2),
            judge_score=audit["score"], judge_verdict="FAIL", judge_feedback="<judge reasoning>",
            judge_model=judge.model if judge else "",
            pass_threshold=threshold, score_scale=scale,
        )
    print(f"--- {args.stage} prompt for {unit.label}, {language} ({prompt.path}) ---\n")
    print(prompt.render(values))
    missing = prompt.missing_placeholders(values)
    if missing:
        print(f"\n[preview] unfilled placeholders: {missing}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="batch_trans", description="Benchmark translation pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("translate", help="stage 1: translate a benchmark")
    _common(p)
    p.add_argument("--limit", type=int, help="only process the first N rows (debugging)")
    p.add_argument("--split", nargs="*", help="only these splits")
    p.add_argument("--dataset-config", nargs="*", help="only these dataset configs")
    p.set_defaults(func=cmd_translate)

    p = sub.add_parser("judge", help="stage 2: judge existing translations")
    _common(p)
    p.add_argument("--input", help="translation records file or directory (default: this run's translate/)")
    p.add_argument("--round", type=int, default=1)
    p.set_defaults(func=cmd_judge)

    p = sub.add_parser("correct", help="stage 3: re-translate what the judge failed")
    _common(p)
    p.add_argument("--input", help="judge records file or directory (default: this run's judge_r<round>/)")
    p.add_argument("--round", type=int, default=1)
    p.set_defaults(func=cmd_correct)

    p = sub.add_parser(
        "recorrect",
        help="re-run correct + judge for whatever still scores below a higher bar",
    )
    _common(p)
    p.add_argument("--threshold", type=float, required=True,
                   help="correct every instance whose newest judgement scored strictly below this")
    p.add_argument("--include-recorrected", action="store_true",
                   help="also re-correct instances an earlier round already corrected "
                        "(default: only the ones the earlier, lower bar walked past)")
    p.add_argument("--correct-round", type=int,
                   help="write to correct_r<N> (default: one past the highest existing round)")
    p.add_argument("--judge-round", type=int,
                   help="write to judge_r<N> (default: one past the highest existing round)")
    p.add_argument("--skip-judge", action="store_true",
                   help="write the corrections but do not re-judge them")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be corrected and exit without calling any model")
    p.add_argument("--split", nargs="*")
    p.add_argument("--dataset-config", nargs="*")
    p.set_defaults(func=cmd_recorrect)

    p = sub.add_parser("assemble", help="stage 4: rebuild the parallel dataset and store or push it")
    _common(p)
    p.add_argument("--input", nargs="*", help="record directories to merge (default: translate/ + correct_r*/)")
    p.add_argument("--destination", choices=["local", "huggingface"], help="override output.destination")
    p.add_argument("--limit", type=int)
    p.add_argument("--split", nargs="*")
    p.add_argument("--dataset-config", nargs="*")
    p.set_defaults(func=cmd_assemble)

    p = sub.add_parser("review", help="stage 5: flat per-instance file for human verification")
    p.add_argument("--config", required=True)
    p.add_argument("--output-root", help="override output.root")
    p.add_argument("--input", nargs="*",
                   help="stage directories to read (default: translate/ + judge_r*/ + correct_r*/)")
    p.add_argument("--fail-threshold", type=float,
                   help="queue anything scoring below this (default: review.fail_threshold, "
                        "else the judge's pass_threshold)")
    p.add_argument("--sample-rate", type=float, help="fraction of passing instances to spot-check")
    p.add_argument("--seed", help="sampling seed; same seed picks the same rows")
    p.add_argument("--queue-only", action="store_true", help="skip the full per-instance file")
    p.add_argument("--prefer-best-translation", action="store_true",
                   help="hand over the highest-scoring version of each instance rather than the "
                        "newest one (default: review.prefer_best_translation, else newest)")
    p.add_argument("--csv-all", action="store_true",
                   help="also write <split>.csv with every instance, not just the queue "
                        "(default: review.csv_all_rows, else queue only)")
    p.add_argument("--queue-regressions", action="store_true",
                   help="also queue instances whose newest version scores below their best "
                        "(default: review.queue_regressions, else off)")
    p.add_argument("--split", nargs="*")
    p.add_argument("--dataset-config", nargs="*")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("costs", help="aggregate token usage and cost for a benchmark")
    p.add_argument("--config", required=True)
    p.add_argument("--reprice", action="store_true",
                   help="re-price every stage from its token counts at current config prices, "
                        "not just the ones recorded as $0.00")
    p.add_argument("--no-reprice", action="store_true",
                   help="report exactly what the stages recorded, gaps and all")
    p.set_defaults(func=cmd_costs)

    p = sub.add_parser("run", help="chain stages in one process (convenience only)")
    _common(p)
    p.add_argument("--stages", default="translate,judge,correct,assemble")
    p.add_argument("--rounds", type=int, default=1, help="judge/correct rounds")
    p.add_argument("--destination", choices=["local", "huggingface"])
    p.add_argument("--limit", type=int)
    p.add_argument("--split", nargs="*")
    p.add_argument("--dataset-config", nargs="*")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("preview", help="render a stage prompt for the first row, without calling any model")
    p.add_argument("--config", required=True)
    p.add_argument("--stage", choices=["translate", "judge", "correct"], default="translate")
    p.add_argument("--split", nargs="*")
    p.add_argument("--dataset-config", nargs="*")
    p.set_defaults(func=cmd_preview)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
