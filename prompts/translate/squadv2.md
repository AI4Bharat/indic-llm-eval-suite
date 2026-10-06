You translate English SQuAD v2 reading-comprehension examples into fluent {target_language} in {target_script}.

INPUT
One JSON object with exactly three keys:
- `context` -- the passage, a string
- `question` -- a string
- `answers_text` -- a list of answer strings taken verbatim from `context`; **it is often empty**

OUTPUT
Return exactly one valid JSON object and nothing else:
`{"context":"...","question":"...","answers_text":["...","..."]}`
No Markdown, commentary, answer labels, offsets, or extra keys.

## The answerability contract -- read this first

`answers_text` tells you which kind of example this is, and you must not change which kind it is.

- **Empty list `[]`** means the question is deliberately *unanswerable* from this passage. Return `"answers_text": []`. Never invent an answer, never add an entry, and never adjust the passage so that it starts to support one. These examples usually contain a near-miss that looks like an answer; translate that near-miss faithfully and leave it just as unsupported as it is in English.
- **Non-empty list** means the question is answerable. Return exactly as many entries, in the same order.

Never change the number of entries in `answers_text`. That count is the label.

## The extractive contract

Every string in the returned `answers_text` must appear in the returned `context` as an exact, contiguous substring -- character for character, including internal spacing and punctuation.

- Translate the passage first, then take each answer *out of your own translated passage*. Do not translate the answer independently and hope it matches.
- Keep answers minimal and extractive. Do not add case markers, postpositions, articles, honorifics, quantifiers, explanations, or trailing punctuation that is not part of the span itself. A span that reads slightly clipped in isolation is correct; a fluent noun phrase that is not a literal run of the passage is wrong.
- If the same English string appears more than once in `answers_text` (annotators frequently agree word for word), translate it to the **same** target string every time. Divergent duplicates break scoring.
- When an answer phrase occurs several times in the passage, translate every occurrence identically so the span is still findable.
- If a faithful translation would make a span impossible to keep contiguous, adjust the *passage* wording -- within the bounds of a faithful translation -- so that the span exists, rather than padding or paraphrasing the answer.

## Translation

- Translate every natural-language span in the context and question into precise, natural {target_language}. Preserve every fact, entity, pronoun reference, tense, negation, comparison, temporal and causal relation, quotation status, ambiguity, and source error.
- Translate the full context without summarising, omitting, reordering, or repairing. A clause that looks incidental is often exactly what makes a question answerable or not.
- Use the least-committal natural grammatical form when English gender, number, definiteness, or reference is underspecified. Do not resolve an ambiguity English leaves open.
- Preserve identity for personal, place, and organisation names, using conventional target-script transliteration. Do not culturally localise the passage.

## Exact preservation

- Preserve ASCII digits and their order, dates, currencies, units, formulas, signs, variables, code, URLs, file paths, abbreviations, acronyms, quotations, and protected punctuation exactly. Never render source ASCII digits in native-script digits.
- Preserve paragraph and line structure and any whitespace an answer span depends on. For right-to-left scripts, keep protected ASCII content in logical order and add no bidirectional control characters.

## Silent preflight

Before responding, confirm: `answers_text` has exactly the input's length; every entry is an exact contiguous substring of your translated `context`; repeated English answers map to identical target strings; no unanswerable question has acquired an answer; protected content is unchanged; the output is valid JSON with exactly the three required keys.

INPUT JSON:
{source_json}
