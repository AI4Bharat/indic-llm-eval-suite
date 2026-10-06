"""Cost and token reporting across a run.

Each stage writes a ``costs.json`` next to its records; this rolls them up to
language, split, benchmark, stage and whole-run level and prints a table.

Stages price their requests as they run, which means a model the price table did
not know at the time is recorded at $0.00.  Rather than leave that permanently
wrong, the report re-prices from the *token counts*, which are always recorded
correctly: add the model to the config's ``pricing:`` block and re-run ``costs``.
Each stage's ``by_mode`` split says which requests ran in batch mode, so the 50%
batch discount is applied to exactly those.  ``reprice="force"`` re-prices
everything at current prices; the default only fills in the $0.00 gaps.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import paths
from .config import Config
from .costs import USAGE_KEYS, estimate_cost, format_total, lookup_price, merge_totals
from .jsonl import write_json


def _blank() -> dict:
    return {"requests": 0, **{k: 0 for k in USAGE_KEYS}, "cost_usd": 0.0}


def _price(total: dict, model: str, batch: bool, pricing: dict) -> float:
    cost, _ = estimate_cost(
        model,
        int(total.get("input_tokens") or 0),
        int(total.get("output_tokens") or 0),
        int(total.get("reasoning_tokens") or 0),
        batch=batch,
        pricing_overrides=pricing,
    )
    return cost


def reprice(data: dict, cfg: Config, force: bool) -> bool:
    """Recompute this stage's cost from its token counts. True if anything changed.

    Modifies ``data`` in place.  Language groups carry no mode breakdown of their
    own, so they are scaled by the same batch/interactive blend as their stage --
    exact when a stage ran entirely in one mode, which is the normal case.
    """
    total = data.get("total") or {}
    tokens = sum(int(total.get(k) or 0) for k in ("input_tokens", "output_tokens", "reasoning_tokens"))
    if not tokens:
        return False
    if not force and float(total.get("cost_usd") or 0.0) > 0:
        return False

    model = data.get("model")
    if not model or lookup_price(model, cfg.pricing) is None:
        return False

    by_mode = data.get("by_mode") or {}
    if by_mode:
        priced = sum(_price(t, model, mode == "batch", cfg.pricing) for mode, t in by_mode.items())
    else:                                   # no mode recorded: assume interactive rates
        priced = _price(total, model, False, cfg.pricing)

    # one blended factor for the groups, derived from what the modes actually cost
    interactive = _price(total, model, False, cfg.pricing)
    factor = (priced / interactive) if interactive else 1.0

    total["cost_usd"] = round(priced, 6)
    total["cost_source"] = "repriced"
    for mode, t in by_mode.items():
        t["cost_usd"] = round(_price(t, model, mode == "batch", cfg.pricing), 6)
    for group in (data.get("by", {}).get("groups") or {}).values():
        group["cost_usd"] = round(_price(group, model, False, cfg.pricing) * factor, 6)
    return True


def collect(cfg: Config, reprice_mode: str = "gaps") -> dict:
    root = paths.benchmark_dir(cfg)
    by_stage: dict[str, list[dict]] = {}
    by_unit: dict[str, list[dict]] = {}
    by_language: dict[str, list[dict]] = {}
    files = sorted(root.rglob("costs.json"))
    repriced: list[str] = []

    for path in files:
        if path.parent == root:      # the run-level file we ourselves write
            continue
        data = json.loads(path.read_text())
        if reprice_mode != "off" and reprice(data, cfg, force=reprice_mode == "force"):
            repriced.append(f"{data.get('stage', '?')}"
                            f"{'_r' + str(data['round']) if data.get('round') else ''}"
                            f" ({data.get('model')})")
        total = data.get("total") or _blank()
        stage_key = data.get("stage", path.parts[-4] if len(path.parts) >= 4 else "unknown")
        if data.get("round"):
            stage_key = f"{stage_key}_r{data['round']}"
        by_stage.setdefault(stage_key, []).append(total)
        by_unit.setdefault(data.get("unit", "unknown"), []).append(total)
        for language, group in (data.get("by", {}).get("groups") or {}).items():
            by_language.setdefault(language or "unknown", []).append(group)

    return {
        "benchmark": cfg.benchmark,
        "sources": [str(p) for p in files],
        "repriced": repriced,
        "run_total": merge_totals([t for totals in by_stage.values() for t in totals]),
        "by_stage": {k: merge_totals(v) for k, v in sorted(by_stage.items())},
        "by_unit": {k: merge_totals(v) for k, v in sorted(by_unit.items())},
        "by_language": {k: merge_totals(v) for k, v in sorted(by_language.items())},
    }


def print_report(report: dict) -> None:
    def section(title: str, groups: dict) -> None:
        if not groups:
            return
        print(f"\n{title}")
        width = max(len(k) for k in groups)
        for key, total in groups.items():
            print(f"  {key:<{width}}  {format_total(total)}")

    print(f"\n=== cost report: {report['benchmark']} ===")
    section("by stage", report["by_stage"])
    section("by unit (config/split)", report["by_unit"])
    section("by language", report["by_language"])
    print(f"\n  {'RUN TOTAL':<24}  {format_total(report['run_total'])}\n")
    if report.get("repriced"):
        print("  priced from token counts at current config prices: "
              + ", ".join(report["repriced"]) + "\n")


def run(cfg: Config, reprice_mode: str = "gaps") -> dict:
    report = collect(cfg, reprice_mode)
    write_json(paths.benchmark_dir(cfg) / "costs.json", report)
    print_report(report)
    return report
