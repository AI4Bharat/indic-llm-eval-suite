"""On-disk layout for pipeline artefacts.

Everything a run produces lives under ``<output.root>/<benchmark>/`` in a flat,
predictable hierarchy so that any stage can be handed to another person as a
directory of JSONL files::

    <root>/<benchmark>/
      source/<config>/<split>/rows.jsonl            full original rows (written by stage 1)
      translate/<config>/<split>/
          requests.jsonl        every request sent, all attempts
          results.jsonl         every raw provider result, all attempts
          records.jsonl         one validated translation per (row, language, group)
          failures.jsonl        requests still unresolved after max_attempts
          costs.json            token / cost totals for this unit
          batch/attempt1/...    batch input files, job state, downloaded output
      judge_r1/<config>/<split>/...                 same shape
      correct_r1/<config>/<split>/...               same shape
      final/<config>/<split>.jsonl                  assembled parallel dataset
      costs.json                                    run-level aggregate
"""

from __future__ import annotations

from pathlib import Path

from .config import Config, Unit


def stage_dirname(stage: str, round_: int) -> str:
    """Directory name for a stage. Judge/correct are round-scoped, translate is not."""
    if stage in ("judge", "correct"):
        return f"{stage}_r{round_}"
    return stage


def benchmark_dir(cfg: Config) -> Path:
    return Path(cfg.output.root) / cfg.benchmark


def unit_dir(cfg: Config, stage: str, unit: Unit, round_: int = 1) -> Path:
    return benchmark_dir(cfg) / stage_dirname(stage, round_) / unit.config / unit.split


def source_dir(cfg: Config, unit: Unit) -> Path:
    return benchmark_dir(cfg) / "source" / unit.config / unit.split


def final_path(cfg: Config, unit: Unit) -> Path:
    ext = "parquet" if cfg.output.format == "parquet" else "jsonl"
    return benchmark_dir(cfg) / "final" / unit.config / f"{unit.split}.{ext}"


def ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
