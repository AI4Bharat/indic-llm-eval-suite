#!/usr/bin/env python3
"""Materialise IMO-AnswerBench as local JSONL from the upstream release.

IMO-AnswerBench (Luong et al., 2025, google-deepmind/superhuman) is 400 olympiad
problems with short verifiable answers: 100 each of Algebra, Combinatorics,
Geometry and Number theory.  The authoritative copy is a CSV in the DeepMind
repo; the Hub copies are third-party mirrors, so this reads the CSV directly.

Columns are renamed to snake_case so output columns read ``problem_hi`` rather
than ``Problem_hi`` and no column name contains a space:

    Problem ID -> id   Problem -> problem   Short Answer -> short_answer
    Category -> category   Subcategory -> subcategory   Source -> source

Only ``problem`` is translated (see ``configs/imo_answerbench.yaml``).
``short_answer`` rides through as an untranslated source column so a translated
problem can be graded against the same key as the English one; it is never sent
to the model.

FIDELITY.  The only edit is stripping leading/trailing whitespace from
``problem`` (the CSV leaves a trailing newline on every statement); the count is
printed.  Everything inside a statement is copied verbatim, source defects
included.

    python scripts/prepare_imo_answerbench.py

Writes ``local_data/imo_answerbench/test.jsonl``.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from batch_trans.jsonl import write_jsonl

URL = "https://raw.githubusercontent.com/google-deepmind/superhuman/main/imobench/answerbench.csv"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "local_data" / "imo_answerbench" / "test.jsonl"
COLUMNS = {
    "Problem ID": "id",
    "Problem": "problem",
    "Short Answer": "short_answer",
    "Category": "category",
    "Subcategory": "subcategory",
    "Source": "source",
}


def fetch() -> list[dict]:
    with urllib.request.urlopen(URL, timeout=120) as r:
        text = r.read().decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text, newline="")))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    raw = fetch()
    missing = set(COLUMNS) - set(raw[0])
    if missing:
        raise SystemExit(f"upstream CSV changed shape; missing columns {sorted(missing)}")
    print(f"[imo_answerbench] fetched {len(raw)} rows")

    out, stripped = [], 0
    for row in raw:
        rec = {new: row[old] for old, new in COLUMNS.items()}
        clean = rec["problem"].strip()
        stripped += clean != rec["problem"]
        rec["problem"] = clean
        out.append(rec)

    if len({r["id"] for r in out}) != len(out):
        raise SystemExit("problem ids are not unique")
    blank = [r["id"] for r in out if not r["problem"]]
    if blank:
        raise SystemExit(f"{len(blank)} rows have an empty problem: {blank[:5]}")

    print(f"[imo_answerbench] categories: {dict(Counter(r['category'] for r in out))}")
    print(f"[imo_answerbench] stripped surrounding whitespace on {stripped} problems")
    write_jsonl(args.out, out)
    print(f"[imo_answerbench] wrote {len(out)} rows -> {args.out} "
          f"(longest problem {max(len(r['problem']) for r in out)} chars)")


if __name__ == "__main__":
    main()
