#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
NAME="${IMAGINE_WORKER_NAME:-imagine-qwen-worker}"

echo "[stop] worker container $NAME"
docker rm -f "$NAME" 2>/dev/null || true

if [[ -f "$ROOT/data/portal.pid" ]]; then
  old=$(cat "$ROOT/data/portal.pid" || true)
  if [[ -n "${old:-}" ]]; then
    echo "[stop] portal pid $old"
    kill "$old" 2>/dev/null || true
  fi
  rm -f "$ROOT/data/portal.pid"
fi

# also kill uvicorn app:app on 7860 if present
pkill -f 'uvicorn app:app --host 0.0.0.0 --port 7860' 2>/dev/null || true
echo "[stop] done"
