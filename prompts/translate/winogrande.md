You translate English WinoGrande fill-in-the-blank examples into fluent {target_language} in {target_script}.

INPUT
One JSON object with exactly three keys:
- `sentence` -- contains exactly one ASCII underscore `_` marking the blank
- `option1`, `option2` -- the two candidate fillers

OUTPUT
Return exactly one valid JSON object and nothing else:
`{"sentence":"...","option1":"...","option2":"..."}`
No answer label, explanation, Markdown, commentary, or extra keys. You are never told which option is correct and must not try to work it out.

## The blank

- `sentence` must contain exactly one ASCII `_`, at the position that plays the same semantic role as in English. Do not replace it with a word, a pronoun, brackets, a different placeholder, or a longer run of underscores, and do not add a second one.
- Place the blank where the target language would naturally put that constituent. Matching English word order is not required; keeping the same grammatical role is.

## Answer neutrality -- the whole point of this benchmark

Both options must remain genuinely insertable. Substituting either one into `_` must yield a sentence that is grammatical, natural, and **equally** so.

This is where {target_language} leaks answers that English hides. Before choosing any construction around the blank, check whether it would encode a property that distinguishes the two options:

- **Gender and number agreement** on verbs, adjectives, participles, or auxiliaries.
- **Case marking and postposition or preposition choice**, including differential object marking and animacy-sensitive forms.
- **Honorific level** and the pronoun or verb form it selects.
- **Classifiers, measure words, and definiteness marking.**
- **Word order or ellipsis** that only resolves under one reading.

Whenever the two options differ in any such feature, choose the most neutral construction available -- an uninflected or common form, a rephrasing that avoids agreement with the blank, or a word order that does not commit. If the options share the feature, ordinary agreement is fine.

Do not resolve the blank, reveal the intended antecedent, or make one option ungrammatical, awkward, redundant, or conspicuous.

## The two options

- Keep them distinct, in their original order, and comparably fluent, specific, and long. Do not merge them, gloss one, or make one more polished or more explanatory than the other.
- Preserve their identity, noun-phrase scope, definiteness, modifiers, possessives, and references.
- Translate each option so that it reads as the same kind of constituent as the other. If one is a bare name and the other a common noun, keep that asymmetry rather than levelling it.

## Faithfulness

- Preserve all facts, roles, actions, causal and temporal relations, negation, comparison, ambiguity, quoted wording, and source errors. Trigger words often decide the answer: keep the exact force of `because`, `although`, `but`, `so`, `since`, `before`, `after`, `not`, `only`, `more`, and `less`.
- Preserve ASCII digits, names, abbreviations, code, URLs, paths, quotations, and protected punctuation exactly. Never render source ASCII digits in native-script digits.
- Use natural {target_language} prose and conventional target-script transliteration for names. Do not culturally localise people, places, or scenarios. For right-to-left scripts, add no bidirectional control characters.

## Silent preflight

Before responding, substitute each option into the blank in turn and read both sentences. Confirm: both are grammatical and comparably natural; neither gets an agreement, case, honorific, classifier, or word-order clue the other lacks; `_` occurs exactly once; option order and identity are unchanged; protected content is unchanged; the output is valid JSON with exactly the three required keys.

INPUT JSON:
{source_json}
