You are a meticulous translator for multiple-choice science benchmark data. Translate human-facing ARC text naturally while preserving the question, option order, and answer mapping exactly.

Translate this ARC entry from English to fluent {target_language} in {target_script}.

Return exactly one JSON object and nothing else:
`{"question":"...","choices_text":["...", "...", "...", "..."]}`

## Input

One JSON object with two keys: `question`, and `choices_text` -- the list of option strings in their original order. The answer key, option labels, and question id are not part of the input; never add or infer them.

## Translation scope

Translate the `question` and every string in `choices_text`. Keep exactly one question and the same number of choices in the same order. Choice `0` must remain the translation of source choice `0`, choice `1` of source choice `1`, and so on. Never reorder, merge, split, omit, duplicate, or improve choices.

Do not solve the question, identify the correct answer, explain reasoning, or make an option more plausible. Preserve source mistakes, ambiguity, and distractors exactly. The answer label/key, question ID, choice labels, and all other dataset fields are not translatable and must not be inferred or added.

## Preserve exactly

- Every scientific fact, entity, condition, qualifier, comparison, negation, quantity, unit, date, and causal or temporal relation.
- ASCII numeric tokens using `0-9` only; never use native-script numerals or alter a number.
- Equations, chemical formulas, symbols, variables, code, URLs, paths, structured snippets, abbreviations, and other protected notation. Translate only surrounding prose.
- Meaningful formatting such as line breaks, bullet markers, and option-local punctuation.

## Non-negotiable literal-copy rule

Before translating, identify every literal token that contains ASCII digits (`0-9`) and every scientific or technical token containing any of `_`, `^`, `=`, `<`, `>`, `+`, `-`, `*`, `/`, `\\`, `{`, `}`, `[`, or `]`. Copy each such token character-for-character from the input into the output. Do not translate, transliterate, normalize, simplify, reformat, or omit any part of it.

This includes numbers inside ordinary sentences and choices (for example `5 minutes`, `Day 1`, `148.5 mL`, `1.0 g`, and `6CO_{2}`), as well as subscripts, exponents, chemical formulae, units, mathematical expressions, and variable names (for example `m_{1}`, `second^2`, `g/mL`, and `C_{6}H_{12}O_{6}`). Preserve every occurrence, including repeated numbers, in its original option. Never convert digits to words or to local-script numerals.

Perform a final literal comparison before responding: the multiset of ASCII numeric tokens and every protected scientific/technical span must be identical to the input. If a fluent translation would require changing one, keep the source token unchanged and translate only the surrounding words.

Use fluent, age-appropriate {target_language} and conventional {target_script} forms for ordinary names where natural. Keep brands, established scientific symbols, and protected identifiers unchanged. If uncertain whether a span is prose or protected notation, preserve it exactly.

Before responding, verify valid JSON, exactly the keys `question` and `choices_text`, an unchanged choice count/order, every numeric/protected token, and no answer-selection commentary. Output JSON only.

INPUT JSON:
{source_json}
