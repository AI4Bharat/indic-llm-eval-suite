#!/usr/bin/env python3
"""Materialise the text-only subset of Humanity's Last Exam as local JSONL.

HLE's no-tools configuration is text-only: the model answers from the question
alone, with no web, no code execution and no image.  ``cais/hle`` ships all 2,500
questions including the multi-modal ones, so the subset has to be cut here --
a question whose statement refers to a figure we are not carrying is unanswerable
and untranslatable, and there is no config on the Hub that has it removed.
Rows are kept when ``image`` is empty, which is HLE's own marker for a text-only
question.

``cais/hle`` is a *gated* repo: HF_TOKEN must belong to an account that has
accepted the terms on https://huggingface.co/datasets/cais/hle, or this fails
with a 403.

Only ``id``, ``question``, ``answer``, ``answer_type``, ``rationale``,
``raw_subject``, ``category``, ``author_name`` and ``canary`` are written out.
``image``, ``image_preview`` and ``rationale_image`` are dropped: the first is
empty by construction for every kept row, and the other two are binary blobs
(158 MB of the 274 MB download) that JSONL cannot hold and that a text-only
subset has no use for.

Keep ``canary``.  It is how HLE detects training contamination and it should
travel with any derived copy -- the same rule this repo already follows for GPQA.

Only ``question`` is translated (see ``configs/hle_no_tools.yaml``).  ``answer``
stays English: for ``exactMatch`` it is graded by string comparison, and for
``multipleChoice`` it is a bare option letter.

    python scripts/prepare_hle_no_tools.py

Writes ``local_data/hle_no_tools/test.jsonl``.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from batch_trans.jsonl import write_jsonl

REPO = "cais/hle"
PARQUET = "datasets/cais/hle/data/test-00000-of-00001.parquet"
# 'image' is needed for the filter and then discarded; the two binary image
# columns are never read, which is what keeps this a ~116 MB fetch not 274 MB.
READ_COLUMNS = ["id", "question", "image", "answer", "answer_type",
                "author_name", "rationale", "raw_subject", "category", "canary"]
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "local_data" / "hle_no_tools" / "test.jsonl"


def load() -> list[dict]:
    import pyarrow.parquet as pq
    from huggingface_hub import HfFileSystem

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    fs = HfFileSystem(token=token)
    with fs.open(PARQUET, "rb") as handle:
        table = pq.ParquetFile(handle).read(columns=READ_COLUMNS)
    return table.to_pylist()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep-multimodal", action="store_true",
                    help="do not drop questions that carry an image")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    rows = load()
    print(f"[hle_no_tools] loaded {len(rows)} rows from {REPO}")

    if not args.keep_multimodal:
        kept = [r for r in rows if not (r.get("image") or "").strip()]
        print(f"[hle_no_tools] dropping {len(rows) - len(kept)} multi-modal rows, keeping {len(kept)}")
        rows = kept

    out = [{k: v for k, v in row.items() if k != "image"} for row in rows]

    if len({r["id"] for r in out}) != len(out):
        raise SystemExit("id is not unique")
    blank = [r["id"] for r in out if not (r["question"] or "").strip()]
    if blank:
        raise SystemExit(f"{len(blank)} rows have an empty question: {blank[:5]}")

    write_jsonl(args.out, out)
    print(f"[hle_no_tools] answer_type: {dict(Counter(r['answer_type'] for r in out))}")
    print(f"[hle_no_tools] category: {dict(Counter(r['category'] for r in out).most_common())}")
    longest = max(len(r["question"]) for r in out)
    print(f"[hle_no_tools] wrote {len(out)} rows -> {args.out} (longest question {longest} chars)")


if __name__ == "__main__":
    main()
