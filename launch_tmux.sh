#!/usr/bin/env bash
# Full launch sequence inside tmux session `imagine`.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
chmod +x run_worker.sh run_portal.sh stop_all.sh launch_tmux.sh

SESSION=imagine

# 1) Stop MiniMax H3 to free ~70GB
for c in vllm-minimax-h3; do
  if docker ps --format '{{.Names}}' | grep -qx "$c"; then
    echo "[launch] stopping $c"
    docker stop "$c"
  else
    echo "[launch] $c not running"
  fi
done

# tmux session
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "[launch] reusing tmux session $SESSION"
else
  tmux new-session -d -s "$SESSION" -n worker
  tmux new-window -t "$SESSION" -n portal
fi

tmux send-keys -t "$SESSION:worker" C-c "" Enter 2>/dev/null || true
sleep 1
tmux send-keys -t "$SESSION:worker" "cd $ROOT && ./run_worker.sh" Enter

echo "[launch] waiting for worker health…"
for i in $(seq 1 180); do
  if curl -sf http://127.0.0.1:7861/health 2>/dev/null | grep -q '"ready": true\|"ready":true'; then
    echo "[launch] worker ready"
    break
  fi
  sleep 5
  if [[ $i -eq 180 ]]; then
    echo "[launch] worker not ready in time" >&2
    exit 1
  fi
done

tmux send-keys -t "$SESSION:portal" C-c "" Enter 2>/dev/null || true
sleep 1
tmux send-keys -t "$SESSION:portal" "cd $ROOT && ./run_portal.sh" Enter
sleep 3

echo "[launch] curl status / homepage"
curl -skf https://127.0.0.1:7860/api/status | head -c 500 || curl -sf http://127.0.0.1:7860/api/status | head -c 500
echo
code=$(curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1:7860/ || curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7860/)
echo "homepage HTTP $code"

if [[ "${IMAGINE_SMOKE:-1}" == "1" ]]; then
  echo "[launch] optional 512x512 smoke"
  curl -sk -X POST https://127.0.0.1:7860/api/generate \
    -H "Content-Type: application/json" -H "X-Imagine-Pin: ${IMAGINE_PIN:-haku}" \
    -d '{"prompt":"a red apple on a table, simple","width":512,"height":512,"steps":8,"seed":1}' \
    | head -c 800 || true
  echo
fi

echo "[launch] done. tmux attach -t imagine"
