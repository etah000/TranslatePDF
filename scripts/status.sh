#!/usr/bin/env bash
# Print TranslatePDF service status: pid, alive?, port listener.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PID_FILE="$PROJECT_DIR/run/server.pid"
CONFIG_FILE="${PDF2ZH_CONFIG:-$PROJECT_DIR/config.json}"

# Read port from config.json (default 8765) without a python dependency.
port="$(grep -oE '"port"[[:space:]]*:[[:space:]]*[0-9]+' "$CONFIG_FILE" 2>/dev/null \
    | head -1 | grep -oE '[0-9]+' || true)"
port="${port:-8765}"

status="stopped"
if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE")"
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
        status="running (pid=$PID)"
    else
        status="stale pid file (pid=$PID not alive)"
    fi
fi

echo "Service:  $status"
echo "Port:     $port"

if command -v ss >/dev/null 2>&1; then
    if ss -ltn 2>/dev/null | grep -qE ":$port\b"; then
        echo "Listener: yes (port $port bound)"
    else
        echo "Listener: no"
    fi
fi

# Liveness probe if reachable.
if command -v curl >/dev/null 2>&1; then
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 \
        "http://127.0.0.1:$port/health" 2>/dev/null || echo "000")"
    echo "Health:   HTTP $code  (GET /health)"
fi