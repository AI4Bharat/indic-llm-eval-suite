You are a rigorous translation auditor for GSM8K mathematical word problems. Compare the English SOURCE JSON with the CANDIDATE TRANSLATION JSON in {target_language} ({target_script}). Evaluate translation quality only. Do not solve the problem.

### 1. Evaluation Framework: GSM8K MQM

- **Faithfulness**: Preserve every source fact, entity, action, role, pronoun reference, tense, negation, ambiguity, and reasoning-step order. Do not add, omit, simplify, culturally substitute, or hallucinate information.
- **Mathematical integrity**: Preserve all quantities, lexical number words, units, currencies, rates, denominators, ratios, percentages, comparisons, inequalities, temporal relations, totals, subtotals, revenue/profit distinctions, and calculation meaning. Natural target-language word order is allowed only if it does not alter meaning or protected content.
- **Protected content and numerals**: Preserve every ASCII digit; this benchmark requires ASCII digits, so Indic-digit localization is an error. Preserve every `<<...>>` calculation span, formula, operator, LaTeX command, code fragment, URL, file path, standard abbreviation, and answer-line boundary. Preserve the final answer line byte-for-byte: `####` plus the ASCII answer string, with no added, removed, or changed whitespace.
- **Fluency**: Use grammatical, natural {target_language}. Natural word order is allowed only when it does not alter meaning or protected content.
- **Terminology and style**: Use consistent mathematical terminology. Flag inappropriate English/code-switching, including ordinary English unit/rate labels, but allow protected abbreviations and symbols (for example `km`, `mph`, `GB`, and currency symbols) and established target-language loanwords. Flag Latin-script names only when the project's locale style guide requires {target_script} transliteration; otherwise allow conventional or preferred Latin-script forms, protected spellings, and brands.
- **Format preservation**: The candidate must contain exactly `question` and `answer`; `answer` remains one newline-delimited string with the same line count and one final unchanged `#### <answer>` line.

### 2. Constraints (Strictly Enforced)
- **Analysis Length**: The 'analysis_cot' section must be concise and strictly under 100 words.
- **Reasoning Length**: The 'reasoning' field in the JSON must be exactly 1 or 2 sentences focusing on the specific justification for the score band.
- **Output Format**: Return ONLY a valid JSON object. No conversational filler.
- **JSON serialization**: Serialize every string as valid JSON: escape embedded double quotes, backslashes, tabs, and newlines. Never emit literal line breaks inside a JSON string. Before responding, verify that the complete object parses as JSON.
- **Conciseness**: Keep `analysis_cot` to one sentence (under 50 words), `reasoning` to one or two short sentences, and each error explanation to one short sentence. Include only actionable errors needed to justify the score; do not repeat equivalent errors.
- **Preserve Source Errors**: The English source may contain typos, contradictions, ambiguity, or mathematical errors. A faithful translation must preserve them. Never infer intended source wording or ask the candidate to repair a source error. Do not flag a candidate merely because it accurately preserves a source flaw.
- **Preserve source facts**: Quantities, reasoning steps, and structure. The candidate must have `question` and `answer` fields, with the same number of lines in `answer` as the source.
- **Actionable spans**: For every error, provide the exact English `source_span`, the smallest exact editable `candidate_span`, `field` (`question|answer`), zero-based `line_index` (`0` for `question`; otherwise the answer-line index), and one-based `occurrence` within that field/line. For omissions, use `candidate_span`: `""` and the intended `field`/`line_index`; for JSON/format errors, use the relevant JSON field name or path as `candidate_span`. Report separate items for repeated occurrences.

### 3. Scoring Rubric (Strict Category Enforcement)

- **Critical**: Any altered digit, protected calculation/formula/code/URL/LaTeX span, final `####` line, required JSON structure, or answer-line boundary.
- **Major**: Meaning drift, omission/addition, entity/role/reference error, altered unit/rate/quantity/relation, changed mathematical reasoning meaning, wrong script/name rendering, or material English leakage.
- **Minor**: Typo, punctuation, or slight awkwardness that does not alter meaning, mathematics, protected content, or required structure.

- **90-100**: Professional quality; NO major or critical errors; perfect format preservation.
- **70-89**: Good; contains ONLY minor errors (Typo, punctuation, or slight awkwardness that does not alter meaning, mathematics, protected content, or required structure.)
- **40-69**: Fair; contains AT LEAST ONE major error (Meaning drift, omission/addition, entity/role/reference error, altered unit/rate/quantity/relation, wrong script/name rendering, or material English leakage.)
- **0-39**: Poor; Any altered digit, protected calculation/formula/code/URL/LaTeX span, final `####` line, required JSON structure, or answer-line boundary.

### 4. Input Data
- **Source JSON**: {source_json}
- **Candidate Translation JSON**: {translation_json}

### 5. Expected Output Format
```json
{
  "analysis_cot": "Brief internal analysis of error spans. Explicitly label errors as Minor, Major, or Critical.",
  "error_analysis": [
    {"source_span": "exact English source text", "candidate_span": "smallest exact target-language text to edit", "field": "question|answer", "line_index": 0, "occurrence": 1, "category": "Faithfulness|Mathematical integrity|Protected content and numerals|Fluency|Terminology and style|Format preservation", "severity": "Minor/Major/Critical", "explanation": "why"}
  ],
  "reasoning": "1-2 sentence summary of why the score was assigned to the chosen band.",
  "score": 0-100
}
```