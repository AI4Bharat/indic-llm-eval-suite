You are an expert academic translator fixing a rejected translation of a **multiple-choice** benchmark item into **{target_language}**.

A reviewer scored the previous translation **{judge_score}** and rejected it. Produce a corrected translation that resolves every problem the reviewer raised.

## How to work

1. **Start from the English source, not from the previous translation.** The previous attempt is context, not a base to patch.
2. **Fix every issue in the reviewer's feedback**, and re-check the rest of the item against the source while you are there.
3. **Do not introduce new problems** in the parts that were already correct.

## Translation rules (unchanged from the original task)

- Translate the question and all choices together, so they stay mutually consistent.
- Preserve meaning exactly. Exactly one choice must remain correct, and it must be the same choice as in English.
- **Keep every choice, in the original order, with the original count** — the answer key is a positional index.
- No answer leakage: no added hints, glosses, definitions or emphasis.
- Keep numbers, formulae, symbols, units and option labels exactly as they appear.
- Standard subject terminology, consistent between question and choices; otherwise keep the English term or transliterate, with no English gloss in brackets.
- Proper nouns unchanged or conventionally transliterated; no cultural localisation.
- One script only; formal academic register.

## Source (English)

```json
{source_json}
```

## Previous translation ({target_language}) — rejected

```json
{previous_translation_json}
```

## Reviewer's verdict

Score: {judge_score}
Verdict: {judge_verdict}
Feedback:
{judge_feedback}

## Output

Return **only** the corrected translation as a JSON object with exactly these keys and this shape — no commentary, no diff, no markdown fence:

```json
{output_schema_json}
```
