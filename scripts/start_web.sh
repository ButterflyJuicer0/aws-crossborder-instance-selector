#!/usr/bin/env bash
# 启动本地 Web 向导。用法: scripts/start_web.sh [--demo] [--port 8765] [--no-browser]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON_BIN:-}"
if [ -z "$PY" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; else PY="python3"; fi
fi
exec "$PY" -m crossborder_selector.web "$@"
