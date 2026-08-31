#!/usr/bin/env sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/_common.sh"

API_URL=${EXPERIMENT_API_URL:-http://127.0.0.1:18765/api/experiment}

require_docker

before=$(curl -fsS "$API_URL")
if ! printf '%s' "$before" | grep -q '"experiment_id": "llm-trading-lab-exp1"'; then
  echo "runtime check 失败：GET /api/experiment identity 不匹配。" >&2
  exit 3
fi

if docker compose up --help 2>/dev/null | grep -q -- '--wait'; then
  compose --profile experiment up -d --no-deps --force-recreate --wait --wait-timeout 120 api web
else
  compose --profile experiment up -d --no-deps --force-recreate api web
fi

after=$(curl -fsS "$API_URL")
if [ "$before" != "$after" ]; then
  echo "runtime check 失败：API/Web recreate 前后 ExperimentLedger projection 不一致。" >&2
  exit 3
fi

echo "runtime / archives named volumes 在 API/Web recreate 后保持同一 ledger state。"
