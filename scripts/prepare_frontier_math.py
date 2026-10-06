#!/usr/bin/env python3
"""Scrape Epoch AI's public FrontierMath sample problems into local JSONL.

FrontierMath is not distributable.  Epoch AI holds the 338-problem set (295 in
Tiers 1-3, 43 in Tier 4) privately so that it cannot leak into training data, it
is on no Hugging Face repo, and the only problems in the open are the handful
published as samples on

    https://epoch.ai/frontiermath/tiers-1-4/benchmark-problems

which is what this scrapes: every sample problem across all four tiers, with its
tier, title, statement, final answer and full worked solution.  Answers and
solutions are collected so a translated problem can be checked against the same
ground truth the English one is graded on -- they are carried as untranslated
source columns, never sent to the model.

Only ``problem`` is translated (see ``configs/frontier_math.yaml``).

FIDELITY.  The page is the source of record and is copied verbatim, defects
included.  One Tier-1 statement renders its display equation as ``[x^3y+y^3z+
z^3x=0]`` with the LaTeX ``\\[`` delimiter already stripped on Epoch's side; that
is preserved as-is rather than repaired, the same way every prompt in this repo
preserves a source error instead of fixing it.  Inline maths arrives as
``\\(...\\)`` and code blocks as fenced Python, both of which the translate
prompt treats as protected.

The page is static HTML with no API behind it, so this is a real scrape and will
need revisiting if Epoch restyles the page.  ``--html`` runs it against a saved
copy instead of the network.

    python scripts/prepare_frontier_math.py
    python scripts/prepare_frontier_math.py --tier 4

Writes ``local_data/frontier_math/sample.jsonl``.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from batch_trans.jsonl import write_jsonl

URL = "https://epoch.ai/frontiermath/tiers-1-4/benchmark-problems"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "local_data" / "frontier_math" / "sample.jsonl"

TIER = re.compile(r'<h2 id="tier-(\d+)"[^>]*>.*?</h2>', re.DOTALL)
TITLE = re.compile(r'<h3 class="display-4"[^>]*>(.*?)</h3>', re.DOTALL)
PANEL_OPEN = re.compile(r'<div style="display: (?:block|none);" class="problem-content-scroll[^"]*">')
DIV = re.compile(r"<div\b|</div>")
PRE = re.compile(r'<pre[^>]*?(?:data-language="([^"]*)")?[^>]*>(.*?)</pre>', re.DOTALL)
LINE = re.compile(r'<span class="line">(.*?)</span></span>|<span class="line"></span>', re.DOTALL)
# Two spellings appear on the page -- '<strong>Answer:</strong>' (11 problems) and
# '<strong>Answer</strong>:' (1) -- and two problems carry no 'Solution:' marker at
# all, running the worked solution straight on from the answer paragraph.
ANSWER = re.compile(r"<strong>\s*Answer\s*:?\s*</strong>\s*:?\s*")
SOLUTION = re.compile(r"<strong>\s*Solution\s*:?\s*</strong>\s*:?\s*")
PARAGRAPH_END = re.compile(r"</p>")
SLUG = re.compile(r"[^a-z0-9]+")


def panels(block: str) -> list[str]:
    """The Problem and Solution panels, matched by counting div depth.

    A panel is not ``<div ...>(.*?)</div>``: statements wrap display equations in
    a nested ``<div class="mline-height">``, and a non-greedy match closes on that
    inner div instead, silently truncating the problem at its first equation.
    """
    out: list[str] = []
    for open_match in PANEL_OPEN.finditer(block):
        depth, position = 1, open_match.end()
        for tag in DIV.finditer(block, open_match.end()):
            depth += 1 if tag.group(0) == "<div" else -1
            if depth == 0:
                position = tag.start()
                break
        else:
            raise SystemExit("unterminated problem panel -- the page layout has changed")
        out.append(block[open_match.end():position])
    return out


def split_answer(panel: str) -> tuple[str, str]:
    """``(answer, solution)`` out of a solution panel.

    The answer is the remainder of the paragraph the ``Answer:`` marker opens, and
    the solution is everything after it -- which is the only reading that also
    works for the two problems that never write ``Solution:``.
    """
    answer_match = ANSWER.search(panel)
    if not answer_match:
        return "", panel
    rest = panel[answer_match.end():]
    solution_match = SOLUTION.search(rest)
    if solution_match:
        return rest[:solution_match.start()], rest[solution_match.end():]
    paragraph = PARAGRAPH_END.search(rest)
    if paragraph:
        return rest[:paragraph.start()], rest[paragraph.end():]
    return rest, ""


def strip_tags(fragment: str) -> str:
    """HTML fragment -> plain text, keeping LaTeX, code fences and paragraphs."""
    def code_block(match: re.Match) -> str:
        language, body = match.group(1) or "", match.group(2)
        lines = [strip_tags_inline(m.group(1) or "") for m in LINE.finditer(body)]
        if not lines:
            lines = strip_tags_inline(body).splitlines()
        return "\n\n```" + language + "\n" + "\n".join(lines).rstrip() + "\n```\n\n"

    text = PRE.sub(code_block, fragment)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</p>\s*|</li>\s*|</div>\s*", "\n\n", text)
    text = re.sub(r"<li[^>]*>", "- ", text)
    return normalise(strip_tags_inline(text))


def strip_tags_inline(fragment: str) -> str:
    """Drop every remaining tag and unescape entities, leaving the text alone.

    ``<span class="mjx-inline">\\(x\\)</span>`` becomes ``\\(x\\)``: the wrapper is
    presentation, the delimiters are the mathematics.
    """
    return html.unescape(re.sub(r"<[^>]+>", "", fragment))


def normalise(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse(page: str) -> list[dict]:
    """One record per sample problem, tagged with the tier heading above it."""
    bounds = [(int(m.group(1)), m.end()) for m in TIER.finditer(page)]
    if not bounds:
        raise SystemExit("no '<h2 id=\"tier-N\">' headings found -- the page layout has changed")
    bounds.append((None, len(page)))

    records: list[dict] = []
    seen: Counter[str] = Counter()
    for (tier, start), (_, end) in zip(bounds, bounds[1:]):
        section = page[start:end]
        titles = list(TITLE.finditer(section))
        for i, title_match in enumerate(titles):
            block_end = titles[i + 1].start() if i + 1 < len(titles) else len(section)
            block = section[title_match.end():block_end]
            title = normalise(strip_tags_inline(title_match.group(1)))
            found = panels(block)
            if len(found) < 2:
                raise SystemExit(f"tier {tier}: expected a Problem and a Solution panel for "
                                 f"{title!r}, found {len(found)}")
            answer, solution = split_answer(found[1])

            slug = SLUG.sub("-", title.lower()).strip("-")[:60]
            seen[slug] += 1
            suffix = f"-{seen[slug]}" if seen[slug] > 1 else ""

            records.append({
                "id": f"frontiermath-tier{tier}-{slug}{suffix}",
                "tier": tier,
                "title": title,
                "problem": strip_tags(found[0]),
                "answer": strip_tags(answer).rstrip("."),
                "solution": strip_tags(solution),
                "source_url": URL,
            })
    return records


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tier", type=int, choices=[1, 2, 3, 4], action="append",
                    help="keep only these tiers (repeatable); default is all four")
    ap.add_argument("--html", type=Path, help="parse a saved copy of the page instead of fetching")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    if args.html:
        page = args.html.read_text(encoding="utf-8")
    else:
        request = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=120) as r:
            page = r.read().decode("utf-8")

    records = parse(page)
    print(f"[frontier_math] scraped {len(records)} sample problems")
    print(f"[frontier_math] per tier: {dict(sorted(Counter(r['tier'] for r in records).items()))}")

    if args.tier:
        records = [r for r in records if r["tier"] in set(args.tier)]
        print(f"[frontier_math] keeping tiers {sorted(set(args.tier))}: {len(records)} problems")

    if not records:
        raise SystemExit("nothing to write")
    if len({r["id"] for r in records}) != len(records):
        raise SystemExit("generated ids are not unique")
    for record in records:
        for field in ("problem", "answer"):
            if not record[field].strip():
                raise SystemExit(f"{record['id']}: empty {field} -- the page layout has changed")

    write_jsonl(args.out, records)
    longest = max(len(r["problem"]) for r in records)
    print(f"[frontier_math] wrote {len(records)} rows -> {args.out} "
          f"(longest problem {longest} chars)")


if __name__ == "__main__":
    main()
