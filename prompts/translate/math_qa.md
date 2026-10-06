You are an expert translator preparing a **mathematics** benchmark for use in **{target_language}**. Each item contains a math problem and its worked solution.

Translate the problem and the solution **together**, so that the notation, variable names and terminology in the solution keep matching the problem.

## Do

1. **Preserve every mathematical expression exactly**: numbers, operators, variables (`x` stays `x`), equations, LaTeX, and inline calculator annotations such as `<<48/2=24>>`. Translate only the surrounding prose.
2. **Preserve the final-answer marker.** If the solution ends with `#### 72`, the translation must end with `#### 72`, digits unchanged.
3. **Preserve the step structure**: same number of lines, same order, same reasoning. Do not merge, split, summarise or "improve" steps.
4. **Use standard {target_language} mathematical vocabulary** (derivative, prime number, hypotenuse, slope…). Be consistent within the item. Where no standard term exists, keep the English term or transliterate it.
5. **Keep units and quantities as written** (cm, m², %, radians, currency symbols).
6. **Keep the register formal**, as in a textbook or exam paper.

## Don't

1. **Don't rewrite notation as words** unless that is genuinely standard in {target_language}.
2. **Don't solve, verify, correct or explain.** If the source solution contains an error, translate the error faithfully.
3. **Don't change constants or named results.** π, e, i, Pythagoras, Euler stay as they are (transliterated where conventional).
4. **Don't code-switch** or add English glosses in brackets.

## Input

Target language: {target_language}

```json
{source_json}
```

## Output

Return **only** a JSON object with exactly these keys and this shape — no commentary, no markdown fence:

```json
{output_schema_json}
```
