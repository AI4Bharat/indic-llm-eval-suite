#!/usr/bin/env python3
"""Materialise Arena-Hard-Auto v2.0 as local JSONL, English prompts only.

``lmarena-ai/arena-hard-auto`` cannot be read with ``load_dataset``: the repo is
a results archive holding 324 JSONL files (every model's answers and every
judge's verdicts), and asking ``datasets`` for it tries to concatenate all of
them into one split, which fails on the schema clash.  The questions themselves
live in exactly two files, so this fetches the one we want directly.

WHY ENGLISH ONLY.  v2.0's 750 prompts are not all English -- 246 of them are
Chinese, Russian, Vietnamese, Japanese, Spanish, Korean and eighteen other
languages, carried over from the live Arena traffic the set was mined from.
Translating those into an Indian language is a different task from the one the
translate/judge/correct prompts were written for: the source-fidelity rules
assume an English source, and a Chinese -> Hindi pass has failure modes (and
needs a reviewer) that an English -> Hindi pass does not.  The ``language``
column is the benchmark's own label, so the filter is the dataset's judgement,
not ours.  Pass ``--all-languages`` to keep all 750.

v0.1 (500 prompts, uniformly English, shorter) is available as ``--version
arena-hard-v0.1`` if the older leaderboard is what you need.

    python scripts/prepare_arena_hard.py
    python scripts/prepare_arena_hard.py --version arena-hard-v0.1

Writes ``local_data/arena_hard/test.jsonl``.  ``uid`` is unique across the set
and is what ``configs/arena_hard.yaml`` uses as ``id_field``.
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

REPO = "lmarena-ai/arena-hard-auto"
URL = "https://huggingface.co/datasets/{repo}/resolve/main/data/{version}/question.jsonl"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "local_data" / "arena_hard" / "test.jsonl"


def fetch(url: str) -> list[dict]:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=300) as r:
        body = r.read().decode("utf-8")
    # split("\n"), not splitlines(): several prompts embed U+2028, U+0085 and
    # form feeds, which splitlines() treats as line breaks and which would tear
    # a JSON string in half.
    return [json.loads(line) for line in body.split("\n") if line.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", default="arena-hard-v2.0",
                    choices=["arena-hard-v2.0", "arena-hard-v0.1"])
    ap.add_argument("--all-languages", action="store_true",
                    help="keep non-English prompts too (v2.0 only; 750 instead of 504)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    rows = fetch(URL.format(repo=REPO, version=args.version))
    print(f"[arena_hard] fetched {len(rows)} prompts from {REPO}/{args.version}")
    by_language = Counter(r.get("language", "unknown") for r in rows)
    print(f"[arena_hard] languages: {dict(by_language.most_common(6))}"
          f"{' ...' if len(by_language) > 6 else ''}")

    if not args.all_languages:
        kept = [r for r in rows if r.get("language") == "English"]
        print(f"[arena_hard] keeping {len(kept)} English prompts, dropping {len(rows) - len(kept)}")
        rows = kept

    uids = {r["uid"] for r in rows}
    if len(uids) != len(rows):
        raise SystemExit(f"uid is not unique ({len(uids)} distinct for {len(rows)} rows)")

    write_jsonl(args.out, rows)
    longest = max(len(r["prompt"]) for r in rows)
    print(f"[arena_hard] wrote {len(rows)} rows -> {args.out} (longest prompt {longest} chars)")


if __name__ == "__main__":
    main()
