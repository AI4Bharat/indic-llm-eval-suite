#!/usr/bin/env bash
#
# Build the human-review files for every benchmark, covering every round on disk.
#
# `review` discovers rounds by directory name, so the re-correction pass
# (correct_r2 / judge_r3, and correct_r4 / judge_r5 where a batch job had to be
# resubmitted) folds in with no extra flags. What this script pins down is the
# shape of what goes out to reviewers:
#
#   <split>.jsonl        every instance, with its full history on one line
#   <split>.csv          every instance, as a spreadsheet, one column per
#                        judging round's score            (--csv-all)
#   <split>.queue.jsonl  what still needs a human, and why
#   <split>.queue.csv    the same, as a spreadsheet
#   summary.json         counts, mean scores, what regressed
#
# Usage:
#   scripts/run_reviews.sh                     # every finished benchmark
#   scripts/run_reviews.sh gsm8k gpqa          # just these
#   THRESHOLD=98 scripts/run_reviews.sh        # queue anything below this
#   SAMPLE_RATE=0.1 scripts/run_reviews.sh     # spot-check fraction of the passing ones
#   PREFER_BEST=1 scripts/run_reviews.sh       # hand over the best-scoring version
#
# Env:
#   THRESHOLD      queue anything scoring below this (default 98, the new bar)
#   SAMPLE_RATE    fraction of passing instances to spot-check (default: config)
#   SEED           sampling seed (default: config)
#   PREFER_BEST=1  hand reviewers the highest-scoring version, not the newest --
#                  worth it because a second correction round can score worse
#                  than the first; without it the newest version is shown and
#                  the regression is merely flagged
#   NO_CSV_ALL=1   only the queue spreadsheet, not the full one
#   LOG_DIR        where per-benchmark logs go (default logs/)

set -uo pipefail
cd "$(dirname "$0")/.."          # repo root: configs/, data/ and batch_trans/ are relative to it

THRESHOLD="${THRESHOLD:-98}"
LOG_DIR="${LOG_DIR:-logs}"

# A benchmark is reviewable once it has translations on disk.
finished_benchmarks() {
  local cfg name
  for cfg in configs/*.yaml; do
    name="$(basename "$cfg" .yaml)"
    case "$name" in
      local_jsonl_example|*_retry) continue ;;
    esac
    if [ -n "$(find "data/${name}/translate" -name records.jsonl -size +0c \
                    -print -quit 2>/dev/null)" ]; then
      echo "$name"
    fi
  done
}

if [ "$#" -gt 0 ]; then
  BENCHMARKS=("$@")
else
  mapfile -t BENCHMARKS < <(finished_benchmarks)
fi

if [ "${#BENCHMARKS[@]}" -eq 0 ]; then
  echo "no translated benchmarks found under data/ -- nothing to review" >&2
  exit 1
fi

FLAGS=(--fail-threshold "$THRESHOLD" --queue-regressions)
[ "${NO_CSV_ALL:-0}" = "1" ]     || FLAGS+=(--csv-all)
[ "${PREFER_BEST:-0}" = "1" ]    && FLAGS+=(--prefer-best-translation)
[ -n "${SAMPLE_RATE:-}" ]        && FLAGS+=(--sample-rate "$SAMPLE_RATE")
[ -n "${SEED:-}" ]               && FLAGS+=(--seed "$SEED")

mkdir -p "$LOG_DIR"
echo "=== building review files at threshold ${THRESHOLD} for: ${BENCHMARKS[*]}"

declare -a OK=() FAILED=()

for name in "${BENCHMARKS[@]}"; do
  config="configs/${name}.yaml"
  if [ ! -f "$config" ]; then
    echo "!!! $name: no such config: $config" >&2
    FAILED+=("$name")
    continue
  fi

  log="${LOG_DIR}/${name}_review_$(date +%Y%m%d_%H%M%S).log"
  echo
  echo "--- $name  (log: $log)"
  if python -m batch_trans.cli review --config "$config" "${FLAGS[@]}" 2>&1 | tee "$log"; then
    OK+=("$name")
  else
    echo "!!! $name failed -- see $log" >&2
    FAILED+=("$name")
  fi
done

echo
echo "=== done. ok: ${OK[*]:-none}"
for name in "${OK[@]}"; do
  python3 - "$name" <<'PY'
import json, sys
name = sys.argv[1]
s = json.load(open(f"data/{name}/review/summary.json"))
for u in s["units"]:
    print(f"    {name}/{u['unit']:<22} {u['instances']:>8,} instances  "
          f"{u['queued']:>7,} queued ({u['queued_pct']}%)  "
          f"mean {u['mean_final_score']}  regressed {u['regressed']}")
PY
done
[ "${#FAILED[@]}" -gt 0 ] && { echo "=== failed: ${FAILED[*]}" >&2; exit 1; }
exit 0
