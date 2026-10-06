# indic-llm-eval-suite

A config-driven pipeline for translating LLM evaluation benchmarks (GSM8K,
ARC-Challenge, MMLU-Pro, …) into many languages, judging the translations, and
correcting the ones that fail.

Two things shape the design:

- **Adding a benchmark means adding a YAML config and prompt files.** The core
  pipeline never learns about a specific benchmark.
- **Every stage is a separate program that reads files and writes files.** You
  can run stage 1 here, email the output to a colleague who runs stage 2 on
  their own machine with their own model, take their files back, and run stage 3
  — no shared process, no shared state, no shared database.

## Stages

| Stage | Command | Reads | Writes |
|---|---|---|---|
| 1. Translate | `translate` | source benchmark (Hub or local JSONL) | `translate/**/records.jsonl` |
| 2. Judge | `judge` | translation records | `judge_r<N>/**/records.jsonl` |
| 3. Correct | `correct` | judge records | `correct_r<N>/**/records.jsonl` |
| 4. Assemble | `assemble` | original rows + translation/correction records | `final/**/<split>.jsonl`, or a push to the Hub |
| 5. Review | `review` | every stage's records | `review/**/<split>.jsonl` + `.queue.jsonl` + `.queue.csv` |
| — Re-correct | `recorrect` | the newest judgement of every instance | a further `correct_r<N>/` + `judge_r<N+1>/` |

Stage 3's output has the same shape as stage 1's, so a correction round can be
fed straight back into stage 2 for re-judging, and stage 4 treats translations
and corrections interchangeably (the newest correction wins).

Stages 4 and 5 are two views of the same records and neither depends on the
other. Stage 4 is the *dataset*: one row per benchmark item, one column per
language. Stage 5 is the *audit trail*: one row per translated instance, with an
explicit `language` field and the full history — first translation, judge round
1, correction, judge round 2 — on that single line. It is the file you hand to a
human verifier.

### Stage 5: the human-review queue

```bash
python -m batch_trans.cli review --config configs/gsm8k.yaml
```

Two things go into the queue:

* **everything still failing** — final score below `review.fail_threshold`
  (default: the judge's own `pass_threshold`) after the *last* judging round
  that saw it, plus anything never judged at all;
* **a spot check of the rest** — `review.sample_rate` of the passing instances
  (25% by default).

Sampling is a hash of the record id, not `random`, so re-running the stage hands
the reviewer exactly the same rows, and a second reviewer can be given the same
sample by naming the seed rather than by copying a file. Every queued row also
says *why* it is there, in `review_reason`.

One flag worth knowing: `final_score_stale` is `true` when a correction round
ran but was never re-judged, so `final_score` describes text that no longer
exists. Those rows go to the queue regardless of score, because nothing has
verified them.

```yaml
review:
  fail_threshold: 70              # default: the judge's pass_threshold
  sample_rate: 0.25               # fraction of passing instances to spot-check
  sample_seed: "gsm8k-human-review-v1"
  csv: true                       # also write the queue as a spreadsheet
  include_judge_details: true     # keep the judge's full reasoning in the history
  include_all_rows: true          # write the full per-instance file, not just the queue
  csv_all_rows: false             # also write <split>.csv with *every* instance, not just the queue
  human_fields: [human_verdict, human_score, human_issue, human_comments, reviewer]
  # only meaningful once a benchmark has more than one correction round --
  # see "Raising the bar after the fact" below
  prefer_best_translation: false  # hand over the highest-scoring version, not the newest
  queue_regressions: false        # queue rows whose newest version scores below their best
```

The CSV is the *queue* by default — that is what a reviewer is asked to act on.
Turn on `csv_all_rows` (or pass `--csv-all`) when the whole set goes out for
human verification rather than just the flagged tail: it writes
`<split>.csv` alongside `<split>.queue.csv`, with one row per instance and one
`judge_r<N>_score` column per judging round, so every score a translation was
ever given is on the line.

```bash
scripts/run_reviews.sh                     # every benchmark, threshold 98, full CSV
scripts/run_reviews.sh gsm8k gpqa          # just these
PREFER_BEST=1 scripts/run_reviews.sh       # hand over the best-scoring version
```

A benchmark-wide spreadsheet is the right thing to archive and the wrong thing to
hand out — mmlu_pro's is 418 MB, and language is the unit of work anyway. Split
it per language, with a read-back check that every row survived:

```bash
python scripts/split_review_csv.py data/mmlu_pro/review/default/test.csv
python scripts/split_review_csv.py data/*/review/*/*.queue.csv     # or every queue at once
```

That writes `by_language/<benchmark>_<split>_<Language>.csv` plus an
`index.json` giving each part's row and queued counts. It splits with the `csv`
module, not by lines: review cells hold translated text with embedded newlines,
and `split -l` would tear rows in half — in mmlu_pro one 12,022-row part spans
17,077 physical lines.

`human_fields` are written blank on every row — in the JSONL under
`human_review`, and as the last columns of the CSV — for the reviewer to fill
in. Overrides for a one-off pull without touching the config:

```bash
python -m batch_trans.cli review --config configs/gsm8k.yaml \
    --fail-threshold 90 --sample-rate 0.1 --seed round2-reviewers --queue-only
```

## Raising the bar after the fact: `recorrect`

A correction round only picks up what `stages.correct.max_score` told it to pick
up at the time it ran. Raising that bar later should not mean re-translating a
whole benchmark: almost everything already clears the new bar, and the only work
worth paying for is the tail the old bar walked past.

```bash
# what would run, without sending a single request
python -m batch_trans.cli recorrect --config configs/gsm8k.yaml --threshold 98 --dry-run

# the real thing: correct everything below 98, then re-judge exactly that
python -m batch_trans.cli recorrect --config configs/gsm8k.yaml --threshold 98
```

For every instance it takes the **newest** judgement on disk — `judge_r2` for
something a correction round already touched, `judge_r1` for everything else —
and keeps the ones scoring strictly below the threshold. Two things are left
out, and the run log says how many of each:

* `already_corrected` — an earlier round already corrected this instance. It was
  not missed, it was tried, and this is what the retry produced; re-trying it is
  a separate decision, `--include-recorrected`.
* `correction_never_judged` — the newest judgement predates the newest
  correction, so its score describes text that no longer exists. Re-judge that
  correction round first; there is no sound basis for a rewrite until you have.

The selection is written out as an ordinary judge-records directory under
`recorrect_r<N>/input/`, so the correction and re-judging that follow are the
same `correct` and `judge` stages every other round uses — no special-cased
inference path. Output lands in the next free rounds (`correct_r2` and
`judge_r3` if you have already run one round of each), which `assemble`,
`review` and `costs` pick up with no extra flags. `recorrect_r<N>/manifest.json`
records the threshold, the rule, the counts and every selected record id.

To do the whole set in one go:

```bash
scripts/rerun_low_scores.sh                     # every finished benchmark
scripts/rerun_low_scores.sh gsm8k gpqa          # just these
DRY_RUN=1 scripts/rerun_low_scores.sh           # print the plan, call nothing
THRESHOLD=95 REVIEW=1 scripts/rerun_low_scores.sh
```

A benchmark counts as finished when it has a config and at least one judging
round with records — there is nothing to re-correct without scores. Each
benchmark gets its own log under `logs/`, and one failure does not stop the
rest.

### What this changes in `review`

`review` finds rounds by directory name, so the new ones fold in with no extra
flags. What a second correction round *adds* is the possibility that the newest
version of a translation is worse than an earlier one — a corrector handed an
already-good translation can still damage it. Every review row therefore also
carries:

| Field | Meaning |
|---|---|
| `score_history` | one entry per judging round: score, verdict, and which version it judged |
| `best_score`, `best_judge_round`, `best_stage`, `best_round` | the highest-scoring version and where it came from |
| `recorrected`, `correction_rounds_applied` | whether this instance went through a second pass, and which rounds |
| `latest_score`, `translation_picked` | the newest score, and which rule chose the translation shown |

and `review/summary.json` gains `recorrected`, `correction_rounds_used`,
`mean_best_score`, `below_threshold_at_best`, `regressed` and
`stale_final_score`.

Two switches act on that. **Both are off by default**, so a benchmark that only
ever ran one correction round reviews exactly as it did before they existed —
same queue, same rows, byte-identical CSV:

```yaml
review:
  prefer_best_translation: false   # hand over the highest-scoring version, not the newest
  queue_regressions: false         # queue rows whose newest version scores below their best
```

```bash
python -m batch_trans.cli review --config configs/gsm8k.yaml \
    --fail-threshold 98 --prefer-best-translation --queue-regressions
```

The extra CSV columns (`judge_r<N>_score` per round, plus `best_score`,
`correction_rounds`, `recorrected`, `score_history`) appear only when a benchmark
actually has a second correction round, so existing spreadsheets keep their shape.

## Install

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in the Vertex settings and HF_TOKEN
```

### Credentials

Everything authenticates through a **Vertex AI service account** — there are no
API keys anywhere in this pipeline.

All Vertex settings come from the **environment**, never from a benchmark
config. Which project you bill, which region you run in, which key file you hold
and which bucket you stage through are properties of your machine and your
account, not of GSM8K — so configs stay safe to commit, and you can run a
colleague's config against your own project without editing it.

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` (or `PROJECT_ID`) | — | project that is billed |
| `GOOGLE_CLOUD_LOCATION` (or `LOCATION`) | `us-central1` | region for batch jobs |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | service account JSON; unset = ADC |
| `GCS_BUCKET` | — | staging bucket, batch mode only |
| `GCS_PREFIX` | `batch_trans` | key prefix inside that bucket |

`.env` is loaded automatically. The service account needs:

| Role | On | For |
|---|---|---|
| `roles/aiplatform.user` | the project | submitting and polling batch prediction jobs |
| `roles/storage.objectAdmin` | the staging bucket | batch input and output files |

Batch prediction stages input and output through GCS, so `GCS_BUCKET` is
required for batch mode and must be in a region compatible with
`GOOGLE_CLOUD_LOCATION`. Parallel mode needs no bucket.

## Quick start

```bash
# see exactly what will be sent to the model, without spending anything
python -m batch_trans.cli preview   --config configs/gsm8k.yaml --stage translate

# stage 1 on 20 rows to sanity-check the config end to end
python -m batch_trans.cli translate --config configs/gsm8k.yaml --limit 20

# the real thing
python -m batch_trans.cli translate --config configs/gsm8k.yaml
python -m batch_trans.cli judge     --config configs/gsm8k.yaml
python -m batch_trans.cli correct   --config configs/gsm8k.yaml
python -m batch_trans.cli assemble  --config configs/gsm8k.yaml --destination huggingface
python -m batch_trans.cli review    --config configs/gsm8k.yaml

python -m batch_trans.cli costs     --config configs/gsm8k.yaml
```

`run` chains stages in one process as a convenience — it uses the same file
interfaces, so it is not a different code path:

```bash
python -m batch_trans.cli run --config configs/gsm8k.yaml \
    --stages translate,judge,correct,assemble,review --rounds 1
```

Handing work over is just pointing `--input` somewhere else:

```bash
# they received your translate/ directory and nothing else
python -m batch_trans.cli judge   --config configs/gsm8k.yaml --input received/translate

# you received their judge_r1/ directory back
python -m batch_trans.cli correct --config configs/gsm8k.yaml --input received/judge_r1
```

Useful overrides on every stage command: `--mode batch|parallel`, `--model`,
`--output-root`, `--limit`, `--split`, `--dataset-config`.

## Inference modes

**`batch`** (default) uses **Vertex AI batch prediction**: all requests for a
stage — every row × every language — go into one JSONL, get uploaded to GCS, and
run as one job that is polled to completion, then downloaded from GCS and
parsed. Roughly **50% cheaper** than interactive calls. Job names and GCS URIs
are checkpointed to `batch/attempt<N>/state.json`, so if the process dies (or you
close the laptop) during the hours a large job takes, re-running the same command
resumes polling the existing job instead of paying for a second one.

Files land at
`gs://$GCS_BUCKET/$GCS_PREFIX/<benchmark>/<stage>/<config>/<split>/attempt<N>/`,
and the downloaded predictions are kept locally alongside the inputs.

One wrinkle worth knowing: Vertex batch output carries **no request id** — what
it does carry is the echoed request. Responses are therefore matched back to
rows by hashing the prompt that came back, never by output line order (Vertex
does not promise to preserve it). Requests with byte-identical prompts are
handed responses in submission order, which is safe because an identical prompt
is satisfied equally well by either response. Anything that cannot be matched is
reported and retried rather than guessed at.

**`parallel`** runs requests through [LiteLLM](https://docs.litellm.ai) on a
thread pool, so the same pipeline works with any provider — including local
models. Set `litellm_model` and, for self-hosted servers, `api_base`:

```yaml
inference:
  mode: parallel
  litellm_model: hosted_vllm/google/gemma-3-27b-it
  api_base: http://localhost:8000/v1
  num_workers: 64
  json_mode: false        # many local servers reject response_format
```

For `vertex_ai/*` models the project, location and service-account key are
passed to LiteLLM automatically, so parallel mode authenticates exactly like
batch mode:

```yaml
inference:
  mode: parallel
  litellm_model: vertex_ai/gemini-2.5-flash
  num_workers: 32
```

Modes are per stage, so translating in batch mode while judging on a local Gemma
is a two-line config change (see `configs/mmlu_pro.yaml`).

## Retries

Every response is parsed and validated:

- the response must be a JSON object (or `### field` sections, with
  `output_format: sections`);
- it must contain every requested field;
- each field must keep the source's type, and lists must keep their **length and
  order** — a dropped or reordered MCQ option silently invalidates the answer
  key, so it is treated as a failed request, not a translation.

Anything that fails is collected and re-sent, up to `max_attempts`. **Retries
stay in the configured inference mode** — a failed batch request is retried as a
new, smaller batch, never quietly downgraded to interactive calls at double the
price. Requests that never succeed land in `failures.jsonl` with the raw
response and the exact validation error.

### Falling back to parallel mode

Some batch failures never clear on retry. A project-level throttle
(`REPUTATION_TIER_LOW_...`, quota downgrades) rejects a fixed share of *every*
batch regardless of content, so resubmitting the same requests just re-pays for
the same rejection rate. At a 63% rejection rate, three attempts still strand
25% of the split.

For that case a stage can finish the remainder through a different inference
mode:

```yaml
    max_attempts: 3          # batch attempts
    inference:
      mode: batch
    fallback:
      enabled: true
      max_attempts: 2
      num_workers: 32        # parallel requests in the fallback phase
      # model: gemini-2.5-flash                    # optional; defaults to the stage model
      # litellm_model: vertex_ai/gemini-3.7-flash  # optional; inferred from the model name
```

Only requests still failing after every batch attempt enter the fallback phase.
Generation settings (`temperature`, `max_output_tokens`, `json_mode`,
`thinking_budget`) are inherited from the stage, so only the differences need
writing out, and a bare model name is provider-qualified for LiteLLM
automatically (`gemini-3.7-flash` → `vertex_ai/gemini-3.7-flash`).

This is **off unless a config turns it on**, because the fallback pays full
interactive price — no 50% batch discount. Each record names the mode and model
that actually produced it (`inference_mode`, `model`, `provider_model`), and
`costs.json` carries a `by_mode` breakdown so the two phases are separable:

```json
"by_mode": {"batch":    {"requests": 8, "cost_usd": 0.0},
            "parallel": {"requests": 4, "cost_usd": 0.008}}
```

## Output layout

```
data/<benchmark>/
  source/<config>/<split>/rows.jsonl        snapshot of the original rows
  translate/<config>/<split>/
      requests.jsonl     every request sent, all attempts, full prompt text
      results.jsonl      every raw provider result, all attempts, with usage
      records.jsonl      one validated translation per (row, language, group)
      failures.jsonl     what never validated, and why
      costs.json         tokens and USD for this unit
      batch/attempt1/    batch input, job state, downloaded output
  judge_r1/<config>/<split>/...             same shape
  correct_r1/<config>/<split>/...           same shape
  recorrect_r2/input/<config>/<split>/      judgements re-correct selected, + manifest.json
  correct_r2/<config>/<split>/...           the re-correction pass, same shape
  judge_r3/<config>/<split>/...             and its re-judging, same shape
  final/<config>/<split>.jsonl              assembled parallel dataset
  final/<config>/<split>.missing.jsonl      any (row, language) with no translation
  review/<config>/<split>.jsonl             one row per instance, full history
  review/<config>/<split>.csv               the same, as a spreadsheet (csv_all_rows)
  review/<config>/<split>.queue.jsonl       just what a human should check
  review/<config>/<split>.queue.csv         the same, as a spreadsheet
  review/summary.json                       how many queued, and why
  costs.json                                run-level aggregate
```

`requests.jsonl` and `results.jsonl` are append-only audit logs: re-running a
stage adds to them rather than replacing them. `records.jsonl` is rewritten each
run and is the file downstream stages read.

Every record carries enough to reconstruct what happened: benchmark, dataset
config, split, row id and index, language, field group, source fields, translated
fields, stage, round, attempt count, model and model version, inference mode,
prompt path and content hash, raw response, parsed response, validation status
and error, judge score / verdict / feedback, previous translation, and token
usage with cost.

## Row ids

Every row needs a stable id — it is what ties a translation, a judgement and a
correction back to the same benchmark row, across languages, stages, files and
machines.

If the dataset has an id column, name it in `source.id_field` and it is used
as-is. If it doesn't (GSM8K has only `question` and `answer`), a **UUID is
generated and written into that column**, so the id travels with the data into
every record and into the final dataset.

Generated ids are `uuid5` derived from the benchmark, config, split, row index
and a hash of the row's content — **not** `uuid4`. They have to be reproducible:
stage 2 run next week, or by a colleague on another machine, must land on the
same ids as stage 1 or nothing joins back up. The same id is shared by every
language of a row, so `id` is the column to group on.

## Assembled output

Row *i* of the output is row *i* of the input with one extra column per
(field, language), so all languages stay aligned to the same original row:

| id | question | answer | question_hi | answer_hi | question_ta | … |
|---|---|---|---|---|---|---|

The column name comes from `output.column_template`, which can use `{field}`,
`{language}` (e.g. `Hindi`) and `{code}` (e.g. `hi`). A row with no translation
gets `None` in that column and a line in `<split>.missing.jsonl` — never a
dropped or shifted row.

## Costs

Token counts (input, output, and reasoning/thinking separately) and estimated
USD are recorded per request, then aggregated per language, unit
(config/split), stage and run:

```
=== cost report: gsm8k ===

by stage
  translate     18466 req  in   12,443,102  out    4,201,884  think           0  $2.4574
  judge_r1      18466 req  in   19,882,301  out    1,884,220  think   2,004,118  $31.6…
  correct_r1     1204 req  in    1,402,993  out      388,201  think     412,004  $5.12…
```

Vertex batch prediction is priced at 50% of interactive rates. Prices for models
the built-in table doesn't know (a self-hosted Gemma, a new Gemini release) go in
the config's `pricing:` block, in USD per 1M tokens. In parallel mode LiteLLM's
own cost map is used when it has the model, and the table is the fallback.

A run against an unpriced model records $0.00 — but the *token counts* are always
right, so the price can be supplied after the fact. Add the model to `pricing:`
and re-run `costs`: it re-prices any stage recorded at $0.00 from its tokens,
applying the batch discount to exactly the requests that ran in batch mode (each
stage records a `by_mode` split). Thinking tokens are billed as output.

```bash
python -m batch_trans.cli costs --config configs/gsm8k.yaml               # fill in the $0.00 gaps
python -m batch_trans.cli costs --config configs/gsm8k.yaml --reprice     # re-price everything at today's prices
python -m batch_trans.cli costs --config configs/gsm8k.yaml --no-reprice  # exactly what the stages recorded
```

## Adding a benchmark

1. Write the three prompts, or reuse one of the existing families
   (`generic`, `mcq`, `math_qa`, `gsm8k`, `arc_challenge`, `math_500`, `mbpp`,
   `aime-2026`, `swe-bench`, `mmlu-pro`, `humaneval`, `gpqa`, `squadv2`,
   `winogrande`, `drop`, `jee-bench`, `arena_hard`, `alpaca_eval`,
   `biggenbench`, `frontier_math`, `hle_no_tools`, `harmbench`, `toxicchat`,
   `xstest`, `strongreject`, `fortress`, `imo_answerbench`, `apex_shortlist`) in
   `prompts/{translate,judge,correct}/`. Prompts use `{placeholder}` slots; the
   ones every stage provides are `{target_language}`, `{target_script}` and
   `{source_json}`, plus `{translation_json}` (judge, correct) and `{audit_json}`
   (correct). A field selected by a dotted path reaches the model under its
   flattened name -- `choices.text` becomes `choices_text` -- so name that key,
   not the column, in the prompt's output contract.
2. Copy the closest config from `configs/` and edit `benchmark`, `source`,
   `fields`, `languages`, `output`.
3. `python -m batch_trans.cli preview --config configs/<new>.yaml --stage translate`
   and read what the model will actually receive.
4. `python -m batch_trans.cli translate --config configs/<new>.yaml --limit 20`.

No pipeline code changes.

### Prompt placeholders

Prompts are markdown with `{placeholder}` slots. Only known placeholders are
substituted, so JSON examples inside a prompt (`{"score": 8}`) are left alone.

Available everywhere: every source field by name, `{target_language}`,
`{language_code}`, `{target_script}`, `{source_json}`, `{output_schema_json}`
(a JSON skeleton matching the source's shape — the cheapest way to keep
responses parseable), `{field_names}`.

Judge prompts also get: `{translation_json}`, `{<field>_translation}`,
`{pass_threshold}`, `{score_scale}`, `{translation_model}`.

Correction prompts also get: `{previous_translation_json}` (= `{candidate_json}`),
`{audit_json}` (the judge's full parsed output, including any structured
findings), `{error_analysis_json}`, `{judge_score}`, `{judge_verdict}`,
`{judge_feedback}`, `{judge_model}`.

`{target_script}` resolves through a built-in table of Indian-language writing
systems (Hindi → Devanagari, Punjabi → Gurmukhi, Urdu → Perso-Arabic, …).
Override or extend it per config with `language_scripts:`.

`preview` warns about any placeholder in your prompt that nothing will fill.

## Extractive and blank-filling benchmarks

`squadv2`, `winogrande` and `drop` translate differently from the multiple-choice
and maths benchmarks, in ways worth knowing before you run them or read their
output.

**The answer is part of the text.** SQuAD v2 and DROP are *extractive*: the gold
answer is a literal substring of the passage. Translating the passage and the
answer independently produces an answer that no longer occurs in the passage,
which silently breaks the example. All three prompt stages state the same
contract — translate the passage first, then take each answer out of the
translated passage — and the judge checks the substring relation literally
rather than by meaning.

**Structure carries the label.**

| benchmark | what must not change | why |
|---|---|---|
| `squadv2` | the *length* of `answers_text` | an empty list means the question is unanswerable; 50.1% of the validation split is empty, and adding an entry flips the label |
| `winogrande` | the single ASCII `_` in `sentence` | it marks the blank; both options must stay insertable and equally grammatical |
| `drop` | numeric and date answers, byte for byte | 60% of DROP answers are numerals like `3` or `24-17`, not prose |

`validate_translation` already enforces the list-length half of this for free: a
model that invents an answer for an unanswerable SQuAD question is rejected as
`expected 0 items, got 1` and retried, without any benchmark-specific code.

**Answer leakage is the WinoGrande failure mode.** English hides gender, number
and case where Indic languages mark them. A perfectly idiomatic Hindi sentence
can agree with only one of the two options and hand the reader the answer, so
the prompts require the most neutral construction available around the blank
whenever the options differ in any such feature, and `Answer leakage` is a
critical judge category.

**Answer offsets are not recomputed.** SQuAD's `answer_start` is a character
offset into the *English* context and is carried through unchanged — nothing in
the pipeline can translate an integer. Recover it downstream with
`translated_context.find(translated_answer)`, which is guaranteed to succeed
whenever the extractive contract held. The official SQuAD v2 metric scores EM/F1
over answer *text*, so the standard evaluation path needs no offset at all.

### Shared-passage benchmarks

SQuAD v2 and DROP ask many questions about the same passage:

| benchmark | questions | distinct passages | passage translated |
|---|---|---|---|
| `squadv2` | 11,873 | 1,204 | ~10x more often than necessary |
| `drop` | 9,535 | 579 | ~16x more often than necessary |

The passage is the bulk of every request, so this is the largest single line item
in both configs — roughly $310 of their combined cost — and it also means the
same English passage gets a slightly different translation for each of its
questions. Each output row is self-contained, so this does not break evaluation;
it is a cost and consistency wart, not a correctness one.

Removing it needs a two-pass translate (translate each distinct passage once per
language, then translate each question and its answers against the already
translated passage), which the pipeline does not currently support. It is not
worth building for the other benchmarks, where every row has its own text.

## Benchmarks that need a prep script

Most benchmarks here name a Hub repo in `source.path` and are done. Six cannot:
`arena_hard`, `alpaca_eval`, `biggenbench`, `frontier_math`, `hle_no_tools`
and `imo_answerbench` each go through a `scripts/prepare_*.py` that writes JSONL into `local_data/`,
which the config then reads with `source.type: local`. Run the script once
before the first translate; they are idempotent and re-runnable.

```bash
python scripts/prepare_arena_hard.py      # 504 rows -> local_data/arena_hard/test.jsonl
python scripts/prepare_alpaca_eval.py     # 805 rows -> local_data/alpaca_eval/eval.jsonl
python scripts/prepare_biggenbench.py     # 695 rows -> local_data/biggenbench/test.jsonl
python scripts/prepare_frontier_math.py   #  12 rows -> local_data/frontier_math/sample.jsonl
python scripts/prepare_hle_no_tools.py    # 2,158 rows -> local_data/hle_no_tools/test.jsonl
python scripts/prepare_imo_answerbench.py # 400 rows -> local_data/imo_answerbench/test.jsonl
```

The reason differs each time, and the reason is the interesting part:

| benchmark | why `load_dataset` is not enough |
|---|---|
| `arena_hard` | `lmarena-ai/arena-hard-auto` is a results archive of 324 JSONL files; asking `datasets` for it tries to concatenate model answers with judgements and fails. The questions are one file inside it. |
| `alpaca_eval` | `tatsu-lab/alpaca_eval` is a loading-script dataset, and `datasets` 4.x refuses to run scripts: *"Dataset scripts are no longer supported"*. The data is a plain JSON array in the same repo. |
| `biggenbench` | Loads fine — but 70 of its 765 instances are the `multilingual` capability, written in Korean and *about* Korean, and there is no Hub config with them removed. |
| `frontier_math` | Not distributable and on no Hub repo at all. Only Epoch AI's published sample problems are public, and they are HTML on a web page. |
| `imo_answerbench` | Published only as a CSV in the `google-deepmind/superhuman` repo, with space-containing column names (`Problem ID`, `Short Answer`); the script reads the upstream file and renames them to snake_case. |
| `hle_no_tools` | `cais/hle` is one 2,500-row split mixing text and multi-modal questions, with no config that separates them. It is also **gated** — `HF_TOKEN` must have accepted the terms. |

**They all translate exactly one field.** That is the other thing these six have
in common, and it is deliberate rather than incidental: `prompt`, `instruction`,
`input`, `problem`, `question` and `problem` respectively. Everything else in each record
is either a routing label or an answer key, and both are actively harmful to
send — an answer in the context makes a corrector quietly simplify the problem
it is correcting. So the prompts describe a single-key JSON object, and the
configs carry a one-field group. `test_added_benchmarks` asserts that, because
adding a second field to `fields:` is a one-line change whose consequences are
not visible until a translated answer key ships.

**What is deliberately left in English.** `biggenbench` keeps its
`system_prompt`, `reference_answer` and 1-5 `score_rubric` in English so that a
response generated in-language is still scored against one unchanged rubric
across all 14 languages; a rubric that drifts per language stops measuring one
thing. `alpaca_eval` keeps the baseline `output` the win rate compares against.
`hle_no_tools` keeps `answer`, which is matched by string equality or is a bare
option letter. `frontier_math` keeps `answer` and `solution`. All of them ride
through as source columns, so nothing is lost — they are simply never sent.

### FrontierMath is the public sample, not the benchmark

Worth stating plainly, because the config name does not say it. FrontierMath's
338 problems are held privately by Epoch AI, precisely so they cannot leak into
training data. `configs/frontier_math.yaml` runs against the 12 sample problems
Epoch publishes — 2 of them Tier 4 — scraped from their benchmark-problems page.
That is real FrontierMath text and it exercises the prompts honestly, but 12
rows prove nothing statistically. If a licensed copy of the full set ever
arrives, point `source.files` at it: nothing else has to change.

The scraper copies the page verbatim, defects included. Two statements have
display equations whose LaTeX delimiters are already stripped upstream — they
read `[x^3y+y^3z+z^3x=0]` rather than `\[...\]`. Those stay as they are, the
same way every prompt here preserves a source error rather than fixing it.

## When the answer key lives outside the record

`jee_bench` is the only benchmark here whose answer options are *inside* the
translated string. JEE-Bench has one text column, `question`, carrying the
statement, any LaTeX table or paired list, and — for the 296 multiple-choice
items — the options inline. The key is a separate `gold` column holding a letter
string (`A`, `BD`, `ABD`) or a bare number (`9`, `0.75`, `-14.6`), which is never
translated and never shown to the model.

That inverts where the fragility sits. Elsewhere the pipeline protects the
answer structurally: `validate_translation` enforces the option *count* because
options are a list, and a dropped or reordered option is caught for free. Here
the options are prose, so nothing structural is checked and three things have to
be carried by the prompts alone:

| what must survive | why | how it breaks |
|---|---|---|
| ASCII `A` `B` `C` `D`, in the source's own delimiter style | `gold` is matched against these labels | a label localised to Devanagari or Tamil letters makes the item ungradable, and no stage can tell |
| `(I)`–`(IV)` / `(P)`–`(T)` labels and every `(I) → (P)` mapping | 18 paired-list items encode the answer as a mapping | one re-paired arrow changes which option is correct |
| the requested unit, rounding note and given constants | `gold` is a bare number in those units | "in seconds" dropped, or `g` converted, and the number no longer matches |

Three delimiter styles occur in the data — `(A)` in 257 items, `[A]` in 37, and
a label set inside math mode in 2 — so the prompts require the source's own
style back rather than normalising to one.

**Hedging is part of the question.** `type` marks each item `MCQ` (110, exactly
one correct), `MCQ(multiple)` (186, one or more), `Integer` (82) or `Numeric`
(137), and it is deliberately *not* shown to the model: handing the translator
that label tells it how many options are correct. Instead the prompts require
the source's own hedge — "is/are", "statement(s)", "is(are)", present in 131
items — to come back equally hedged. Many Indian languages have no natural
"is/are", so a translator will quietly pick one; picking the singular on a
multiple-correct item tells the solver to choose exactly one and changes the
task. The judge treats that collapse as critical.

**LaTeX, not prose.** 494 of the 515 items contain math (`\mathrm` 2,417 times,
`\frac` 684, `\left`/`\right` 529 each), and 43 wrap a `tabular` or `array`
environment. Whole environments are copied byte for byte, as in `math_500` —
an unbalanced `\left(` renders as nothing at all.

**Ids.** `index` is the question number within a paper, not a key: 515 rows
share 54 values. It is therefore not the `id_field`, and the pipeline writes a
content-derived UUID into `id` instead; `description` + `index` stay in the
output so a reviewer can still trace a row back to "JEE Adv 2019 Paper 1, Q37".

## Config reference

Configs carry no credentials and no GCP settings — those are environment-only
(see [Credentials](#credentials)).

```yaml
benchmark: gsm8k                  # names the output directory

source:
  type: huggingface               # or: local
  path: openai/gsm8k              # hub repo id
  id_field: id                    # id column; a UUID is generated into it when absent
  cache_dir: null
  revision: null
  configs:                        # HF datasets with configs
    main: [test]
  splits: [test]                  # HF datasets without configs (instead of `configs`)
  files:                          # type: local -- {split: path} or {config: {split: path}}
    test: ../local_data/bench/test.jsonl

fields:                           # field groups translated together, in one request
  default:
    - [question, answer]
  validation:                     # looked up as config/split, split, config, default
    - [question]
  # dotted paths reach into nested columns: [question, choices.text]

languages:                        # a list, or a {name: code} mapping
  Hindi: hi
  Bengali: bn

language_scripts:                 # optional; overrides the built-in script table
  Hindi: Devanagari

stage_defaults:                   # optional; merged into every stage
  inference: {mode: batch, temperature: 0.2}
  max_attempts: 3

stages:
  translate:
    prompt: ../prompts/translate/math_qa.md
    model: gemini-2.5-flash
    output_format: json           # or: sections
    max_attempts: 3               # attempts in the primary mode
    fallback:                     # finish the remainder in another mode; off by default
      enabled: false
      max_attempts: 1
      num_workers: 32             # any inference key is accepted here
      model: null                 # defaults to the stage model
    inference:
      mode: batch                 # batch | parallel
      temperature: 0.2
      max_output_tokens: 8192
      json_mode: true
      thinking_budget: 0          # Gemini; 0 disables thinking
      max_requests_per_batch: 50000
      poll_interval_seconds: 60
      poll_timeout_hours: 48
      # parallel mode only:
      litellm_model: vertex_ai/gemini-2.5-flash
      api_base: null
      api_key_env: null
      num_workers: 32
      num_retries: 3
      request_timeout: 600
      extra_params: {}            # merged verbatim into the LiteLLM call

  judge:
    prompt: ../prompts/judge/math_qa.md
    model: gemini-2.5-pro
    pass_threshold: 7             # below this, the translation goes to stage 3
    score_scale: 10
    enabled: true

  correct:
    prompt: ../prompts/correct/math_qa.md
    model: gemini-2.5-pro
    max_score: null               # correct judgements scoring <= this;
                                  # null = correct whatever the judge failed

output:
  root: ../data
  destination: local              # or: huggingface
  hub_repo: your-org/gsm8k-indic
  private: true
  column_template: "{field}_{language}"     # also {code}
  keep_source_columns: true
  include_quality_columns: false  # per-language stage + judge score columns
  format: jsonl                   # or: parquet

pricing:                          # USD per 1M tokens, for unknown models
  google/gemma-3-27b-it: {input_per_1m: 0.0, output_per_1m: 0.0}
```

## Notes on judging

The judge returns a score, optionally a verdict, and some explanation. A range
of key spellings is accepted, so judge prompts don't have to agree on one
vocabulary:

| Meaning | Keys tried, in order |
|---|---|
| score | `score`, `quality_score`, `rating`, `translation_score`, `overall_score` |
| verdict | `pass`, `passed`, `verdict`, `result`, `status` |
| explanation | `feedback`, `reasoning`, `explanation`, `reason`, `analysis_cot`, `analysis`, `comments`, `critique`, `error_analysis` |

If the verdict is absent it is derived from `pass_threshold`. The scale is
whatever `score_scale` says — the GSM8K judge scores 0–100, the generic ones
0–10. A score outside `0..score_scale` is **rejected and retried**: a judge that
answers `8` against a 0–100 rubric would otherwise send every translation to the
corrector at full price. A judgement that fails a translation **without any
explanation is likewise rejected** — the corrector has nothing to work from.

Whatever the judge returned in full is kept in `judge_parsed` and handed to the
corrector as `{audit_json}`, so a judge that emits structured findings (a list of
error spans with severities, say) gives the corrector far more to act on than a
prose summary.

`max_score` is what a *correction round* selects on; raising it after a round has
already run is what [`recorrect`](#raising-the-bar-after-the-fact-recorrect) is
for, and it takes the new bar as `--threshold` rather than needing the config
edited.

By default only translations the judge explicitly failed are corrected. Setting
`max_score` on the correct stage selects on the score instead — useful when the
judge's `pass_threshold` is more generous than you want to pay to correct, or
when only the clearly-broken tail is worth another round. The rule actually
applied is printed in the run log and recorded in `costs.json` as
`selection_rule`.

Translations the judge could not evaluate at all keep their original text rather
than being rewritten on the strength of a missing verdict; they are listed in
the judge stage's `failures.jsonl`.

## Tests

No network, no API keys — a fake model stands in for the provider.

```bash
python -m tests.test_units      # parsing, validation, batch encode/decode, costs
python -m tests.test_pipeline   # all four stages end to end, incl. retry and handover
python -m tests.test_gsm8k      # the real gsm8k prompts, schema and id generation
python -m tests.test_fallback   # batch rejection wave -> parallel fallback
python -m tests.test_recorrect  # raising the bar after the fact: selection, rounds, review
python -m tests.test_new_benchmarks  # squadv2 / winogrande / drop / jee_bench configs and prompts, end to end
python -m tests.test_added_benchmarks # arena_hard / alpaca_eval / biggenbench / frontier_math / hle_no_tools / imo_answerbench
```

`test_added_benchmarks` reads the JSONL the `scripts/prepare_*.py` scripts
write, and skips any benchmark whose file is missing, naming the script to run.
`test_new_benchmarks` loads Hub datasets, so it needs `datasets` installed.


## Code map

```
batch_trans/
  cli.py             subcommands, argument handling, overrides
  config.py          YAML schema, defaults, validation
  paths.py           where everything is written
  fields.py          field selectors, including dotted paths
  prompts.py         prompt loading, hashing, safe placeholder rendering
  parsing.py         response parsing and validation
  records.py         shared record identity and prompt variables
  llm.py             LLMRequest / LLMResult, backend dispatch
  vertex.py          service-account credentials, genai client, GCS helpers
  vertex_batch.py    upload, submit, poll, download, match, decode
  parallel_infer.py  LiteLLM thread pool
  runner.py          the retry loop shared by all stages
  data.py            source loading, local write, Hub push
  costs.py           token accounting and pricing
  report.py          cost aggregation and the printed report
  stage_translate.py stage 1
  stage_judge.py     stage 2
  stage_correct.py   stage 3
  stage_assemble.py  stage 4
  stage_review.py    stage 5
  rerun.py           the `recorrect` pass: selection, round numbering, manifest
```

```
scripts/
  prepare_*.py            build local_data/ for benchmarks load_dataset cannot serve
  run_<benchmark>.sh      the full translate -> judge -> correct -> judge -> review sequence
  rerun_low_scores.sh     re-correct everything below a new bar, across benchmarks
  run_reviews.sh          rebuild the human-review files across benchmarks
  split_review_csv.py     split a review queue for several reviewers
```
