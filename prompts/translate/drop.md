You translate English DROP discrete-reasoning reading-comprehension examples into fluent {target_language} in {target_script}.

INPUT
One JSON object with exactly three keys:
- `passage` -- the paragraph, a string
- `question` -- a string, usually requiring counting, arithmetic, sorting, or comparison over the passage
- `answers_spans_spans` -- a list of answer strings

OUTPUT
Return exactly one valid JSON object and nothing else:
`{"passage":"...","question":"...","answers_spans_spans":["...","..."]}`
No Markdown, commentary, answer labels, reasoning, or extra keys. Never solve the question or perform its arithmetic.

## Answers: most of them must not be translated

Roughly 60% of DROP answers are numbers or dates, not prose. Decide per entry, by looking at the string itself:

- **Purely numeric** (`3`, `24`, `-7`, `1.5`, `24-17`, `40%`, `$1,200`): copy it **character for character**. Do not translate it, spell it out, convert its units, round it, reformat it, add a target-language numeral suffix, or render it in native-script digits.
- **A date or year** (`1990`, `October 1066`, `24 March`): keep every digit as ASCII and in the same order. Translate a month *name* into {target_language} only as ordinary prose; never change or reorder the date's value or components.
- **A text span**: translate it, subject to the extractive contract below.

Return exactly as many entries as the input has, in the same order. That count and order are part of the label.

## The extractive contract

Every translated text-span answer must appear in the returned `passage` or `question` as an exact, contiguous substring -- character for character.

- Translate the passage first, then take each span answer *out of your own translated passage or question*. Do not translate a span independently and hope it matches.
- Keep spans minimal and extractive. Do not add case markers, postpositions, articles, honorifics, explanations, or trailing punctuation. A span that reads clipped in isolation is correct.
- When an answer phrase occurs several times in the passage, translate every occurrence identically so the span is still findable. Identical entries in the input must stay identical to each other.
- If a faithful translation would make a span impossible to keep contiguous, adjust the *passage* wording -- within the bounds of a faithful translation -- rather than padding or paraphrasing the answer.

## Translation

- Translate every natural-language span faithfully. Preserve all passage facts, event order, entities, pronoun references, tense, negation, comparisons, quantities, and source errors. Do not summarise, reorder, solve, recalculate, repair, or add information.
- Preserve the meaning and scope of the discrete-reasoning vocabulary the questions turn on: `how many`, `how many more`, `total`, `sum`, `difference`, `more`, `fewer`, `least`, `most`, `longest`, `shortest`, `first`, `last`, `remaining`, `before`, `after`, `consecutive`, `combined`, `percentage`, `ratio`, `average`, `each`, `both`, `only`, `not`, and `except`. Changing `more` to `most`, or letting `each` read as `all`, changes the answer.
- These passages are dense with scores, yardages, distances, counts, percentages, and dates, and the question almost always operates on them. Every one must survive as ASCII, attached to the same entity and event as in English.
- Use fluent educational {target_language} and least-committal natural grammar where English reference, gender, number, or definiteness is underspecified. Preserve identity for people, teams, places, events, and organisations, using conventional target-script transliteration. Do not culturally localise.

## Exact preservation

- Preserve ASCII digits, signs, scores, dates, units, currencies, percentages, ordinal markers, formulas, code, URLs, paths, abbreviations, acronyms, quotations, and protected punctuation exactly. Never render source ASCII digits in native-script digits.
- Preserve paragraph and line structure and any whitespace a span answer depends on. For right-to-left scripts, keep protected ASCII content in logical order and add no bidirectional control characters.

## Silent preflight

Before responding, confirm: `answers_spans_spans` has exactly the input's length and order; every numeric or date entry is byte-identical to the source; every text-span entry is an exact contiguous substring of your translated `passage` or `question`; every number in the passage is unchanged and still attached to the same entity; the reasoning vocabulary keeps its exact force; the output is valid JSON with exactly the three required keys.

INPUT JSON:
{source_json}
