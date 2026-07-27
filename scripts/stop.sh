#!/usr/bin/env bash
# Stop the TranslatePDF service started by start.sh.
#
# Sends SIGTERM and waits up to 10s for graceful shutdown (uvicorn handles
# SIGTERM cleanly). Falls back to SIGKILL if it doesn't exit in time.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PID_FILE="$PROJECT_DIR/run/server.pid"

if [[ ! -f "$PID_FILE" ]]; then
    echo "No PID file at $PID_FILE — nothing to stop."
    exit 0
fi

PID="$(cat "$PID_FILE")"
if [[ -z "$PID" ]] || ! kill -0 "$PID" 2>/dev/null; then
    echo "Process $PID not running. Cleaning stale PID file."
    rm -f "$PID_FILE"
    exit 0
fi

echo "Sending SIGTERM to pid $PID ..."
kill -TERM "$PID"

# Wait up to 10s for graceful exit.
for i in $(seq 1 20); do
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "Stopped."
        rm -f "$PID_FILE"
        exit 0
    fi
    sleep 0.5
done

echo "Did not exit in 10s, sending SIGKILL ..."
kill -KILL "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
echo "Killed."