You are an expert translator fixing a rejected translation of an LLM evaluation benchmark item into **{target_language}**.

A reviewer scored the previous translation **{judge_score}** and rejected it. Your job is to produce a corrected translation that resolves every problem the reviewer raised, while keeping everything that was already correct.

## How to work

1. **Start from the English source, not from the previous translation.** The previous attempt is context, not a base to patch.
2. **Fix every issue in the reviewer's feedback.** If the feedback is vague or you disagree with part of it, still re-check that aspect against the source and make the translation unambiguously correct.
3. **Do not introduce new problems.** Everything the reviewer did not complain about should stay as faithful as it already was.

## Translation rules (unchanged from the original task)

- Translate faithfully: no rephrasing, simplifying, explaining, correcting or extending the content.
- Preserve numbers, mathematical expressions, code, variable names, symbols, units, LaTeX, markup and line structure exactly.
- Preserve structure: lists keep their length and order, objects keep their keys.
- Use standard {target_language} terminology; otherwise keep the English term or transliterate. Never add an English gloss in brackets.
- Keep proper nouns as they are, or use the accepted transliteration.
- One script only, apart from numbers, symbols, code and formulae.
- Formal, neutral, academic register.

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
