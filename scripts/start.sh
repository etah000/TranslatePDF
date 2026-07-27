#!/usr/bin/env bash
# Start the TranslatePDF service in the background.
#
# Writes the child PID to run/server.pid and logs to run/server.log.
# Re-running while a previous instance is alive is a no-op (prints PID).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="$PROJECT_DIR/run"
PID_FILE="$RUN_DIR/server.pid"
LOG_FILE="$RUN_DIR/server.log"

mkdir -p "$RUN_DIR"

# Bail if a live instance is already recorded.
if [[ -f "$PID_FILE" ]]; then
    existing="$(cat "$PID_FILE")"
    if [[ -n "$existing" ]] && kill -0 "$existing" 2>/dev/null; then
        echo "Already running (pid=$existing). Use scripts/stop.sh first."
        exit 0
    fi
    rm -f "$PID_FILE"
fi

# Activate conda env if pdf2zh exists and conda is available; otherwise
# rely on whatever python the user invokes. Override with PYTHON env var.
PYTHON_BIN="${PYTHON:-python}"

cd "$PROJECT_DIR"
echo "Starting TranslatePDF (logs -> $LOG_FILE) ..."

# nohup + setsid so the child survives the parent shell exiting.
PYTHONUNBUFFERED=1 nohup "$PYTHON_BIN" -m serve \
    >>"$LOG_FILE" 2>&1 &
PID=$!
echo "$PID" >"$PID_FILE"

# Give uvicorn a moment to bind, then sanity-check.
# 1. Child still alive — catches immediate bind/config errors.
# 2. Port actually accepting connections — distinguishes "started then
#    crashed because port was busy" from "really running".
sleep 1
if ! kill -0 "$PID" 2>/dev/null; then
    echo "Server died during startup. Last log lines:"
    tail -n 40 "$LOG_FILE" || true
    rm -f "$PID_FILE"
    exit 1
fi

# Best-effort port check using /proc/net/tcp (no ss/lsof dependency).
PORT_HEX="$(printf '%04X' "${port:-8765}")"
for _ in 1 2 3 4 5 6 7 8; do
    if grep -qE ":${PORT_HEX} .* 0A " /proc/net/tcp 2>/dev/null; then
        break
    fi
    sleep 0.5
done

echo "Started (pid=$PID). Tail logs: tail -f $LOG_FILE"