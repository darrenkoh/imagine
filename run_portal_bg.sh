#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$ROOT/data"
nohup "$ROOT/run_portal.sh" >"$ROOT/data/portal.log" 2>&1 &
echo $! >"$ROOT/data/portal.pid"
echo "portal pid $(cat "$ROOT/data/portal.pid") log $ROOT/data/portal.log"
