#!/usr/bin/env bash
# Public Imagine portal on 0.0.0.0:7860 with HTTPS (iPhone Safari).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
export IMAGINE_DATA="${IMAGINE_DATA:-$ROOT/data}"
export IMAGINE_PIN="${IMAGINE_PIN:-haku}"
export IMAGINE_WORKER_URL="${IMAGINE_WORKER_URL:-http://127.0.0.1:7861}"
mkdir -p "$IMAGINE_DATA/images" "$IMAGINE_DATA/jobs" "$ROOT/certs"

.venv/bin/python - <<'PY'
import sys
need = []
for m in ("fastapi", "uvicorn", "httpx", "pydantic", "cryptography"):
    try:
        __import__(m)
    except ImportError:
        need.append(m if m != "cryptography" else "cryptography")
if need:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", *need, "pillow"])
PY

.venv/bin/python -c 'from tls import ensure_certs; print("certs:", ensure_certs())'

# kill prior portal on 7860 if we own it
if [[ -f "$ROOT/data/portal.pid" ]]; then
  old=$(cat "$ROOT/data/portal.pid" || true)
  if [[ -n "${old:-}" ]] && kill -0 "$old" 2>/dev/null; then
    echo "[run_portal] stopping pid $old"
    kill "$old" || true
    sleep 1
  fi
fi

# Prefer HTTPS; set IMAGINE_HTTP=1 to force cleartext (Tailscale-only)
if [[ "${IMAGINE_HTTP:-}" == "1" ]]; then
  echo "[run_portal] HTTP on 0.0.0.0:7860 (Tailscale app required on iPhone)"
  exec .venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 7860
else
  CERT="$ROOT/certs/cert.pem"
  KEY="$ROOT/certs/key.pem"
  echo "[run_portal] HTTPS on 0.0.0.0:7860"
  echo "  https://100.123.177.66:7860/"
  echo "  https://spark-7819.tail0182f5.ts.net:7860/"
  echo "  trust page: /trust"
  exec .venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 7860 \
    --ssl-certfile "$CERT" --ssl-keyfile "$KEY"
fi
