#!/usr/bin/env bash
# 一条命令选出跨境最优 EC2 实例。
# 用法: scripts/find_best_instance.sh <region> [batch=10] [rounds=3] [keep=1] [额外 cli 参数...]
# 例:   scripts/find_best_instance.sh ap-east-1 20 3 1 --protect
#       scripts/find_best_instance.sh ap-east-1 2 1 1 --dry-run
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ $# -lt 1 ]; then
  echo "usage: $0 <region> [batch=10] [rounds=3] [keep=1] [extra args...]" >&2
  exit 2
fi
REGION="$1"; BATCH="${2:-10}"; ROUNDS="${3:-3}"; KEEP="${4:-1}"
shift; [ $# -gt 0 ] && shift; [ $# -gt 0 ] && shift; [ $# -gt 0 ] && shift
PY="${PYTHON_BIN:-}"
if [ -z "$PY" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; else PY="python3"; fi
fi
cd "$ROOT"
exec "$PY" -m crossborder_selector.cli select --region "$REGION" --batch-size "$BATCH" \
  --max-rounds "$ROUNDS" --keep-top-k "$KEEP" "$@"
