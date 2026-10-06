#!/usr/bin/env python3
"""Materialise the AlpacaEval evaluation set as local JSONL.

``tatsu-lab/alpaca_eval`` is a *loading-script* dataset (``alpaca_eval.py``), and
``datasets`` 4.x removed script execution entirely -- ``load_dataset`` raises
"Dataset scripts are no longer supported".  The underlying data is a plain JSON
array in the same repo, so this downloads that file directly.

The 805 instructions are identical between AlpacaEval 1.0 and 2.0; only the
baseline the judge compares against differs (``text_davinci_003`` in
``alpaca_eval.json``, ``gpt4_1106_preview`` in ``alpaca_eval_gpt4_baseline.json``).
We translate the ``instruction`` only, so either file gives the same prompts;
the 1.0 file is used because it is the canonical ``alpaca_eval`` config.

``output`` and ``generator`` are carried through untranslated as source columns:
the baseline answer stays English, which is the deliberate scope choice for this
benchmark.  ``dataset`` records which sub-corpus an instruction came from
(selfinstruct, oasst, koala, helpful_base, vicuna).

AlpacaEval ships no id column, and evaluation is positional -- the leaderboard
matches a model's output to the reference by list order.  A generated UUID would
be stable but unreadable and would not carry that ordering, so this assigns
``id`` as ``<sub-dataset>-<zero-padded position in the released file>``, which is
both readable and pins the original order.

    python scripts/prepare_alpaca_eval.py

Writes ``local_data/alpaca_eval/eval.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from batch_trans.jsonl import write_jsonl

URL = "https://huggingface.co/datasets/tatsu-lab/alpaca_eval/resolve/main/{file}"
FILES = {
    "alpaca_eval": "alpaca_eval.json",                          # 1.0 baseline: text_davinci_003
    "alpaca_eval_gpt4_baseline": "alpaca_eval_gpt4_baseline.json",  # 2.0 baseline: gpt4_1106_preview
}
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "local_data" / "alpaca_eval" / "eval.jsonl"


def fetch(file: str) -> list[dict]:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with urllib.request.urlopen(urllib.request.Request(URL.format(file=file), headers=headers),
                                timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="alpaca_eval", choices=sorted(FILES))
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    rows = fetch(FILES[args.config])
    print(f"[alpaca_eval] fetched {len(rows)} rows from {FILES[args.config]}")
    print(f"[alpaca_eval] sub-datasets: {dict(Counter(r['dataset'] for r in rows))}")

    out = []
    for position, row in enumerate(rows):
        out.append({"id": f"{row['dataset']}-{position:04d}", "position": position, **row})

    if len({r["id"] for r in out}) != len(out):
        raise SystemExit("generated ids are not unique")
    blank = [r["id"] for r in out if not r["instruction"].strip()]
    if blank:
        raise SystemExit(f"{len(blank)} rows have an empty instruction: {blank[:5]}")

    write_jsonl(args.out, out)
    longest = max(len(r["instruction"]) for r in out)
    print(f"[alpaca_eval] wrote {len(out)} rows -> {args.out} "
          f"(longest instruction {longest} chars)")


if __name__ == "__main__":
    main()
