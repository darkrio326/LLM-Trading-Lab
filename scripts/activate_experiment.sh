#!/usr/bin/env sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/_common.sh"

EXPECTED_MAIN_SHA=323e0b2dfddea0ce6008c231f88edfd7b08c647e
EXPECTED_PROJECT=llm-trading-lab-exp1
API_URL=${EXPERIMENT_API_URL:-http://127.0.0.1:18765/api/experiment}

require_docker

if [ -n "$(git -C "$PROJECT_ROOT" status --porcelain)" ]; then
  echo "拒绝激活：worktree 不是 clean。" >&2
  exit 3
fi
git -C "$PROJECT_ROOT" fetch --prune origin
if [ "$(git -C "$PROJECT_ROOT" rev-parse main)" != "$EXPECTED_MAIN_SHA" ] ||
   [ "$(git -C "$PROJECT_ROOT" rev-parse origin/main)" != "$EXPECTED_MAIN_SHA" ]; then
  echo "拒绝激活：local main / origin/main 不是 M2 baseline $EXPECTED_MAIN_SHA。" >&2
  exit 3
fi

"$SCRIPT_DIR/verify.sh"

if [ -n "$(git -C "$PROJECT_ROOT" status --porcelain)" ]; then
  echo "拒绝激活：verify 后 worktree 不是 clean。" >&2
  exit 3
fi
git -C "$PROJECT_ROOT" fetch --prune origin
if [ "$(git -C "$PROJECT_ROOT" rev-parse main)" != "$EXPECTED_MAIN_SHA" ] ||
   [ "$(git -C "$PROJECT_ROOT" rev-parse origin/main)" != "$EXPECTED_MAIN_SHA" ]; then
  echo "拒绝激活：verify 后 local main / origin/main 已偏离 M2 baseline。" >&2
  exit 3
fi

project_name=$(compose config | sed -n 's/^name: //p' | head -n 1)
if [ "$project_name" != "$EXPECTED_PROJECT" ]; then
  echo "拒绝激活：Compose project 必须是 $EXPECTED_PROJECT，实际为 $project_name。" >&2
  exit 3
fi

"$SCRIPT_DIR/start.sh" experiment

if [ -n "$(compose --profile experiment ps -q browser)" ]; then
  echo "拒绝激活：experiment profile 不得启动 browser broker service。" >&2
  exit 3
fi

compose exec -T api python -m zhixing.activation prepare \
  --archive-root /opt/zhixing/data/archives \
  --runtime-dir /opt/zhixing/data/runtime

api_id=$(compose ps -q api)
runtime_type=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/opt/zhixing/data/runtime"}}{{.Type}}{{end}}{{end}}' "$api_id")
archives_type=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/opt/zhixing/data/archives"}}{{.Type}}{{end}}{{end}}' "$api_id")
if [ "$runtime_type" != "volume" ] || [ "$archives_type" != "volume" ]; then
  echo "拒绝激活：runtime / archives 必须都是 Docker named volume。" >&2
  exit 3
fi

curl -fsS "$API_URL" >/dev/null

"$SCRIPT_DIR/check_experiment_runtime.sh"

compose exec -T api python -m zhixing.activation start \
  --archive-root /opt/zhixing/data/archives \
  --runtime-dir /opt/zhixing/data/runtime \
  --storage-kind docker_named_volume

started=$(curl -fsS "$API_URL")
if ! printf '%s' "$started" | grep -q '"activation_state": "EXPERIMENT_STARTED"'; then
  echo "拒绝完成：GET /api/experiment 未返回 EXPERIMENT_STARTED。" >&2
  exit 3
fi

echo "EXPERIMENT_STARTED"
