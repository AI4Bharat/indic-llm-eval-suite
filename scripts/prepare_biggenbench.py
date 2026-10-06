#!/usr/bin/env python3
"""Materialise BiGGen-Bench as local JSONL, without the ``multilingual`` capability.

``prometheus-eval/BiGGen-Bench`` loads fine from the Hub; the reason this exists
is the filter.

WHY DROP ``multilingual``.  70 of the 765 instances have ``capability ==
"multilingual"``, and every one of them is already in another language -- the
system prompt and the input are Korean, and the seven tasks under it
(``robust_translation``, ``historical_text_comprehension``, ``cultural_awareness``,
``humor_understanding``, ``poem_writing``, ``global_opinions``,
``multilingual_reasoning``) are *about* the language they are written in.
``robust_translation`` asks the model to translate a Korean idiom into English:
translating its input into Hindi does not produce a Hindi version of the task, it
destroys the task.  Dropping them leaves 695 instances, which is exactly the set
``ai4bharat/biggenbench`` was built from.  Pass ``--keep-multilingual`` to
override.

Only ``input`` is translated (see ``configs/biggenbench.yaml``).  ``system_prompt``,
``reference_answer`` and the six ``score_rubric`` fields ride through as English
source columns, so generation is steered in-language while scoring stays on one
rubric across all 14 languages.

``score_rubric`` is flattened to ``score_rubric_criteria`` and
``score_rubric_score<N>_description`` columns: JSONL keeps the nested dict fine,
but flattening it here means the assembled output and the review spreadsheet
show one rubric level per column instead of one opaque JSON blob per row.

    python scripts/prepare_biggenbench.py

Writes ``local_data/biggenbench/test.jsonl``.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from batch_trans.jsonl import write_jsonl

REPO = "prometheus-eval/BiGGen-Bench"
RUBRIC_KEYS = ("criteria", "score1_description", "score2_description",
               "score3_description", "score4_description", "score5_description")
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "local_data" / "biggenbench" / "test.jsonl"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep-multilingual", action="store_true",
                    help="keep the 70 Korean 'multilingual' instances (765 rows instead of 695)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    import datasets

    rows = datasets.load_dataset(REPO, split="test").to_list()
    print(f"[biggenbench] loaded {len(rows)} rows from {REPO}")
    print(f"[biggenbench] capabilities: {dict(Counter(r['capability'] for r in rows).most_common())}")

    if not args.keep_multilingual:
        kept = [r for r in rows if r["capability"] != "multilingual"]
        print(f"[biggenbench] dropping {len(rows) - len(kept)} 'multilingual' rows, keeping {len(kept)}")
        rows = kept

    out = []
    for row in rows:
        rubric = row.pop("score_rubric") or {}
        missing = [k for k in RUBRIC_KEYS if k not in rubric]
        if missing:
            raise SystemExit(f"{row['id']}: score_rubric is missing {missing}")
        out.append({**row, **{f"score_rubric_{k}": rubric[k] for k in RUBRIC_KEYS}})

    if len({r["id"] for r in out}) != len(out):
        raise SystemExit("id is not unique")
    blank = [r["id"] for r in out if not (r["input"] or "").strip()]
    if blank:
        raise SystemExit(f"{len(blank)} rows have an empty input: {blank[:5]}")

    write_jsonl(args.out, out)
    longest = max(len(r["input"]) for r in out)
    print(f"[biggenbench] wrote {len(out)} rows -> {args.out} (longest input {longest} chars)")


if __name__ == "__main__":
    main()
