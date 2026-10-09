#!/usr/bin/env bash
# Warm Qwen-Image-2.1-Turbo worker in Docker (GPU). Binds only to 127.0.0.1:7861.
# Default schedule is the checkpoint's saved 8-step sample_sigmas grid (CFG 1).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
MODEL="${IMAGINE_MODEL:-/home/dkoh/models/Qwen-Image-2.1-Turbo}"
DATA="${IMAGINE_DATA:-$ROOT/data}"
NAME="${IMAGINE_WORKER_NAME:-imagine-qwen-worker}"
IMAGE="${IMAGINE_IMAGE:-nvcr.io/nvidia/pytorch:25.11-py3}"

mkdir -p "$DATA/images" "$DATA/jobs"

if ! docker info >/dev/null 2>&1; then
  echo "[run_worker] docker is not available; cannot start $NAME" >&2
  exit 1
fi

# Leave any previously downloaded Qwen-Image-2.1 tree alone. Fetch Turbo only
# when this directory has no weights yet.
if [[ ! -f "$MODEL/model_index.json" ]]; then
  echo "[run_worker] $MODEL has no model_index.json; downloading Qwen/Qwen-Image-2.1-Turbo"
  mkdir -p "$MODEL"
  if command -v huggingface-cli >/dev/null 2>&1; then
    huggingface-cli download Qwen/Qwen-Image-2.1-Turbo --local-dir "$MODEL"
  elif command -v hf >/dev/null 2>&1; then
    hf download Qwen/Qwen-Image-2.1-Turbo --local-dir "$MODEL"
  else
    python3 -m pip install -q "huggingface_hub"
    huggingface-cli download Qwen/Qwen-Image-2.1-Turbo --local-dir "$MODEL"
  fi
fi

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "[run_worker] removing existing container $NAME"
  docker rm -f "$NAME" >/dev/null
fi

echo "[run_worker] starting $NAME (model load takes several minutes)"
docker run -d --name "$NAME" --gpus all \
  --ipc=host \
  -p 127.0.0.1:7861:7861 \
  -v "$MODEL:/models/Qwen-Image-2.1-Turbo:ro" \
  -v "$DATA:/data" \
  -v "$ROOT/worker.py:/app/worker.py:ro" \
  -w /app \
  -e IMAGINE_MODEL=/models/Qwen-Image-2.1-Turbo \
  -e IMAGINE_DATA=/data \
  "$IMAGE" \
  bash -lc '
    set -e
    pip uninstall -y torchao 2>/dev/null || true
    # sample_sigmas (saved 8-step Turbo grid) needs current Diffusers.
    # The Qwen3-VL text encoder needs transformers>=5.17.0.
    pip install -q fastapi uvicorn pydantic httpx pillow accelerate "transformers>=5.17.0" \
      && pip install -q git+https://github.com/huggingface/diffusers
    exec uvicorn worker:app --host 0.0.0.0 --port 7861
  '

echo "[run_worker] waiting for /health ready…"
for i in $(seq 1 180); do
  if curl -sf http://127.0.0.1:7861/health | grep -q '"ready":\s*true\|"ready": true'; then
    echo "[run_worker] READY"
    curl -s http://127.0.0.1:7861/health
    echo
    exit 0
  fi
  # still loading?
  if curl -sf http://127.0.0.1:7861/health >/dev/null 2>&1; then
    echo "  … loading ($i)"
  else
    echo "  … starting ($i)"
  fi
  sleep 5
done
echo "[run_worker] timed out waiting for ready" >&2
docker logs --tail 80 "$NAME" || true
exit 1
