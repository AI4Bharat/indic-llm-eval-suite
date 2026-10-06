You are an expert translator fixing a rejected translation of a **mathematics** benchmark item into **{target_language}**. The item is a math problem with its worked solution.

A reviewer scored the previous translation **{judge_score}** and rejected it. Produce a corrected translation that resolves every problem the reviewer raised.

## How to work

1. **Start from the English source, not from the previous translation.** The previous attempt is context, not a base to patch.
2. **Fix every issue in the reviewer's feedback**, and re-verify the untouched parts against the source.
3. **Do not introduce new problems** in the parts that were already correct.

## Translation rules (unchanged from the original task)

- Copy every mathematical expression across untouched: numbers, operators, variables, equations, LaTeX, and inline annotations such as `<<48/2=24>>`. Translate only the surrounding prose.
- Preserve the final-answer marker exactly (`#### 72` stays `#### 72`).
- Keep the same steps, in the same order, with the same count. Do not merge, split or summarise.
- **Translate errors faithfully.** If the source solution is wrong, the translation must reproduce that error — do not solve or fix the problem.
- Use standard {target_language} mathematical vocabulary, consistently across problem and solution; otherwise keep the English term or transliterate, with no English gloss in brackets.
- Keep units and quantities as written; formal textbook register; one script apart from math.

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
