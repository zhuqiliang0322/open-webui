#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/.run/open-webui.pid"
SESSION_FILE="$ROOT_DIR/.run/open-webui.session"
SCREEN_NAME="open-webui-local"

if [[ -f "$SESSION_FILE" ]]; then
    SESSION_VALUE="$(cat "$SESSION_FILE")"
    if [[ "$SESSION_VALUE" == screen:* ]]; then
        if command -v screen >/dev/null 2>&1 && screen -list | grep -q "[.]${SESSION_VALUE#screen:}[[:space:]]"; then
            screen -S "${SESSION_VALUE#screen:}" -X quit >/dev/null 2>&1 || true
            rm -f "$SESSION_FILE" "$PID_FILE"
            echo "open-webui stopped"
            exit 0
        fi
    fi
fi

if [[ ! -f "$PID_FILE" ]]; then
    FALLBACK_PID="$(pgrep -f 'open_webui.main:app --host 127.0.0.1 --port 3000' | tail -n 1 || true)"
    if [[ -n "$FALLBACK_PID" ]]; then
        kill "$FALLBACK_PID"
        rm -f "$PID_FILE" "$SESSION_FILE"
        echo "open-webui stopped"
        exit 0
    fi
    echo "open-webui is not running"
    exit 0
fi

PID="$(cat "$PID_FILE")"

if [[ -z "$PID" ]]; then
    rm -f "$PID_FILE"
    echo "stale pid file removed"
    exit 0
fi

if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    for _ in $(seq 1 30); do
        if ! kill -0 "$PID" 2>/dev/null; then
            rm -f "$PID_FILE" "$SESSION_FILE"
            echo "open-webui stopped"
            exit 0
        fi
        sleep 1
    done
    echo "process $PID did not stop in time"
    exit 1
fi

rm -f "$PID_FILE" "$SESSION_FILE"
echo "stale pid file removed"
