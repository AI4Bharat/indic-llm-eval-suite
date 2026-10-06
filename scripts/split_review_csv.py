#!/usr/bin/env python3
"""Split a review spreadsheet into one file per language (or any other column).

A single benchmark-wide CSV is the right thing for archiving and the wrong thing
for handing out: mmlu_pro's is 418 MB, and no reviewer wants 14 languages
interleaved in it anyway.  Language is also the unit of work -- one reviewer
takes Hindi, another takes Tamil -- so it is the natural seam.

Splitting is done with the ``csv`` module rather than by lines: review cells hold
translated text with embedded newlines, quotes and commas, and a line-based split
would silently tear rows in half.  The file is streamed, never loaded whole.

    python scripts/split_review_csv.py data/mmlu_pro/review/default/test.csv
    python scripts/split_review_csv.py data/*/review/*/*.queue.csv --column language

Writes ``<parent>/by_<column>/<prefix>_<value>.csv`` plus an ``index.json``
listing every part, its row count and how many rows are queued for review.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)

SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def default_prefix(path: Path) -> str:
    """``data/mmlu_pro/review/default/test.queue.csv`` -> ``mmlu_pro_test.queue``.

    The config directory is folded in only when it says something -- "default"
    does not -- because these files get detached from their path the moment they
    are shared, and the name has to stand on its own.
    """
    parts = path.resolve().parts
    benchmark = parts[parts.index("review") - 1] if "review" in parts else path.parent.name
    config = path.parent.name
    stem = path.name[: -len(".csv")]
    pieces = [benchmark] + ([config] if config not in ("default", benchmark) else []) + [stem]
    return SAFE.sub("_", "_".join(pieces))


def row_digest(row: dict, header: list[str]) -> str:
    """A hash of the whole row, for verifying that nothing was altered in transit."""
    h = hashlib.sha256()
    for name in header:
        h.update((row.get(name) or "").encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def split(path: Path, column: str, out_dir: Path | None, prefix: str | None) -> dict:
    out_dir = out_dir or path.parent / f"by_{column}"
    prefix = prefix or default_prefix(path)
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames
        if header is None:
            raise SystemExit(f"{path}: no header row")
        if column not in header:
            raise SystemExit(f"{path}: no '{column}' column (have: {', '.join(header)})")

        handles: dict[str, tuple] = {}
        counts: Counter = Counter()
        queued: Counter = Counter()
        digests: dict[str, list[str]] = defaultdict(list)
        total = 0

        try:
            for row in reader:
                value = row.get(column) or "_blank"
                if value not in handles:
                    dest = out_dir / f"{prefix}_{SAFE.sub('_', value)}.csv"
                    fh_out = open(dest, "w", newline="", encoding="utf-8")
                    writer = csv.DictWriter(fh_out, fieldnames=header, extrasaction="ignore")
                    writer.writeheader()
                    handles[value] = (fh_out, writer, dest)
                _, writer, _ = handles[value]
                writer.writerow(row)
                counts[value] += 1
                if row.get("review_reason"):
                    queued[value] += 1
                digests[value].append(row_digest(row, header))
                total += 1
        finally:
            for fh_out, _, _ in handles.values():
                fh_out.close()

    index = {
        "source": str(path),
        "column": column,
        "rows": total,
        "parts": [
            {
                "value": value,
                "file": str(dest),
                "rows": counts[value],
                "queued": queued[value],
                "bytes": dest.stat().st_size,
            }
            for value, (_, _, dest) in sorted(handles.items())
        ],
    }
    (out_dir / f"{prefix}_index.json").write_text(json.dumps(index, indent=2))
    return {**index, "header": header, "digests": digests, "out_dir": out_dir}


def verify(path: Path, result: dict) -> list[str]:
    """Re-read every part and prove it is the original, rearranged and nothing else."""
    problems: list[str] = []
    header = result["header"]
    seen_total = 0
    seen_digests: dict[str, list[str]] = {}

    for part in result["parts"]:
        dest = Path(part["file"])
        with open(dest, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames != header:
                problems.append(f"{dest.name}: header differs from the source")
                continue
            rows = list(reader)
        if len(rows) != part["rows"]:
            problems.append(f"{dest.name}: wrote {part['rows']} rows, read back {len(rows)}")
        stray = {r.get(result["column"]) for r in rows} - {part["value"]}
        if stray:
            problems.append(f"{dest.name}: contains other {result['column']} values: {sorted(stray)}")
        seen_digests[part["value"]] = [row_digest(r, header) for r in rows]
        seen_total += len(rows)

    if seen_total != result["rows"]:
        problems.append(f"row count: source {result['rows']}, parts total {seen_total}")
    for value, expected in result["digests"].items():
        got = seen_digests.get(value, [])
        if got != expected:
            problems.append(f"{value}: {sum(a != b for a, b in zip(expected, got))} row(s) "
                            f"differ from the source, and {abs(len(expected) - len(got))} missing/extra")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_files", nargs="+", type=Path)
    ap.add_argument("--column", default="language", help="column to split on (default: language)")
    ap.add_argument("--out-dir", type=Path, help="default: <parent>/by_<column>/")
    ap.add_argument("--prefix", help="output filename prefix (default: derived from the path)")
    ap.add_argument("--no-verify", action="store_true", help="skip the read-back check")
    args = ap.parse_args()

    failed = False
    for path in args.csv_files:
        if not path.exists():
            print(f"!!! {path}: no such file", file=sys.stderr)
            failed = True
            continue
        print(f"\n=== {path}  ({path.stat().st_size / 1e6:.1f} MB)")
        result = split(path, args.column, args.out_dir, args.prefix)
        for part in result["parts"]:
            print(f"    {Path(part['file']).name:<48} {part['rows']:>8,} rows "
                  f"{part['queued']:>8,} queued {part['bytes'] / 1e6:>8.1f} MB")
        print(f"    -> {len(result['parts'])} file(s) in {result['out_dir']}")

        if not args.no_verify:
            problems = verify(path, result)
            if problems:
                failed = True
                print("    VERIFY FAILED:", file=sys.stderr)
                for p in problems:
                    print(f"      - {p}", file=sys.stderr)
            else:
                print(f"    verified: {result['rows']:,} rows, every row byte-identical to the source")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
