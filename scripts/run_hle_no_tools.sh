#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."          # repo root: configs/, data/ and batch_trans/ are relative to it

BENCH_NAME=hle_no_tools
CONFIG=configs/${BENCH_NAME}.yaml

# This benchmark reads from local JSONL. Build it first (idempotent):
# python scripts/prepare_hle_no_tools.py

# see the exact prompt first: python -m batch_trans.cli preview --config "$CONFIG" --stage translate
# smoke test:                 python -m batch_trans.cli translate --config "$CONFIG" --limit 20

python -m batch_trans.cli translate --config "$CONFIG"
python -m batch_trans.cli judge     --config "$CONFIG" --round 1
python -m batch_trans.cli correct   --config "$CONFIG" --round 1
python -m batch_trans.cli judge     --config "$CONFIG" --input data/${BENCH_NAME}/correct_r1 --round 2
python -m batch_trans.cli review    --config "$CONFIG"
# python -m batch_trans.cli assemble  --config "$CONFIG"
# python -m batch_trans.cli costs     --config "$CONFIG"
