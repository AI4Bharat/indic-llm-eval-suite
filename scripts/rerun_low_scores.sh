#!/usr/bin/env bash
#
# Re-correct the tail the first pass walked past.
#
# The correction rounds already on disk used each config's own
# `stages.correct.max_score` (70-75 out of 100). The bar is now 98, so for every
# benchmark this re-runs `correct` + `judge` over exactly the instances whose
# newest judgement scored below 98 and that no earlier round already corrected --
# not the whole benchmark, and not work that has already been paid for.
#
# Output lands in correct_r<N+1>/ and judge_r<N+2>/ beside the existing rounds
# (correct_r2 / judge_r3 as things stand), so nothing already written is touched
# and `assemble`, `review` and `costs` pick it up with no extra flags.
#
# Usage:
#   scripts/rerun_low_scores.sh                     # every finished benchmark
#   scripts/rerun_low_scores.sh gsm8k gpqa          # just these
#   DRY_RUN=1 scripts/rerun_low_scores.sh           # print the plan, call nothing
#   THRESHOLD=95 scripts/rerun_low_scores.sh        # a different bar
#   REVIEW=1 scripts/rerun_low_scores.sh            # rebuild the review files after
#
# Env:
#   THRESHOLD            score bar, strictly below (default 98)
#   DRY_RUN=1            print what would run, send no requests
#   INCLUDE_RECORRECTED=1  also re-correct what an earlier round already corrected
#   SKIP_JUDGE=1         write the corrections but do not re-judge them
#   REVIEW=1             run `review` at the same threshold once corrections land
#   MODE=batch|parallel  override inference mode for the correct/judge stages
#   LOG_DIR              where per-benchmark logs go (default logs/)

set -uo pipefail
cd "$(dirname "$0")/.."          # repo root: configs/, data/ and batch_trans/ are relative to it

THRESHOLD="${THRESHOLD:-98}"
LOG_DIR="${LOG_DIR:-logs}"
DRY_RUN="${DRY_RUN:-0}"
INCLUDE_RECORRECTED="${INCLUDE_RECORRECTED:-0}"
SKIP_JUDGE="${SKIP_JUDGE:-0}"
REVIEW="${REVIEW:-0}"

# A benchmark is "finished till now" when it has a config and at least one
# judging round with records -- there is nothing to re-correct without scores.
finished_benchmarks() {
  local cfg name
  for cfg in configs/*.yaml; do
    name="$(basename "$cfg" .yaml)"
    case "$name" in
      local_jsonl_example|*_retry) continue ;;
    esac
    if [ -n "$(find "data/${name}" -path '*/judge_r*' -name records.jsonl -size +0c \
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
  echo "no finished benchmarks found under data/ -- nothing to do" >&2
  exit 1
fi

FLAGS=(--threshold "$THRESHOLD")
[ "$DRY_RUN" = "1" ]             && FLAGS+=(--dry-run)
[ "$INCLUDE_RECORRECTED" = "1" ] && FLAGS+=(--include-recorrected)
[ "$SKIP_JUDGE" = "1" ]          && FLAGS+=(--skip-judge)
[ -n "${MODE:-}" ]               && FLAGS+=(--mode "$MODE")

mkdir -p "$LOG_DIR"
echo "=== re-correcting below ${THRESHOLD} for: ${BENCHMARKS[*]}"
[ "$DRY_RUN" = "1" ] && echo "=== DRY RUN: no requests will be sent"

declare -a OK=() FAILED=()

for name in "${BENCHMARKS[@]}"; do
  config="configs/${name}.yaml"
  if [ ! -f "$config" ]; then
    echo "!!! $name: no such config: $config" >&2
    FAILED+=("$name")
    continue
  fi

  log="${LOG_DIR}/${name}_recorrect_$(date +%Y%m%d_%H%M%S).log"
  echo
  echo "--- $name  (log: $log)"
  if python -m batch_trans.cli recorrect --config "$config" "${FLAGS[@]}" 2>&1 | tee "$log"; then
    OK+=("$name")
  else
    echo "!!! $name failed -- see $log" >&2
    FAILED+=("$name")
    continue
  fi

  if [ "$REVIEW" = "1" ] && [ "$DRY_RUN" != "1" ]; then
    echo "--- $name: rebuilding review at threshold ${THRESHOLD}"
    python -m batch_trans.cli review --config "$config" \
      --fail-threshold "$THRESHOLD" --queue-regressions 2>&1 | tee -a "$log"
  fi
done

echo
echo "=== done. ok: ${OK[*]:-none}"
[ "${#FAILED[@]}" -gt 0 ] && { echo "=== failed: ${FAILED[*]}" >&2; exit 1; }
exit 0
