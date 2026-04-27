#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/bin/python3.11}"
VENV_DIR="$ROOT_DIR/.venv"
RUN_DIR="$ROOT_DIR/.run"
DATA_DIR="$ROOT_DIR/.data"
LOG_FILE="$RUN_DIR/open-webui.log"
PID_FILE="$RUN_DIR/open-webui.pid"
SESSION_FILE="$RUN_DIR/open-webui.session"
SCREEN_NAME="open-webui-local"
HOST_VALUE="${HOST_OVERRIDE:-127.0.0.1}"
PORT_VALUE="${PORT_OVERRIDE:-3000}"

mkdir -p "$RUN_DIR"
mkdir -p "$DATA_DIR"

is_healthy() {
    curl -fsS "http://$HOST_VALUE:$PORT_VALUE/health" >/dev/null 2>&1
}

if [[ -f "$SESSION_FILE" ]]; then
    EXISTING_SESSION="$(cat "$SESSION_FILE")"
    if [[ "$EXISTING_SESSION" == screen:* ]]; then
        if command -v screen >/dev/null 2>&1 && screen -list | grep -q "[.]${SCREEN_NAME}[[:space:]]"; then
            if is_healthy; then
                echo "open-webui is already running at http://$HOST_VALUE:$PORT_VALUE"
                exit 0
            fi
        fi
    elif [[ "$EXISTING_SESSION" == pid:* ]]; then
        EXISTING_PID="${EXISTING_SESSION#pid:}"
        if [[ -n "$EXISTING_PID" ]] && kill -0 "$EXISTING_PID" 2>/dev/null; then
            if is_healthy; then
                echo "open-webui is already running at http://$HOST_VALUE:$PORT_VALUE"
                exit 0
            fi
        fi
    fi
    rm -f "$SESSION_FILE" "$PID_FILE"
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "python3.11 not found at $PYTHON_BIN"
    exit 1
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "missing virtualenv at $VENV_DIR"
    echo "run: uv sync --python $PYTHON_BIN"
    exit 1
fi

if [[ ! -d "$ROOT_DIR/build" ]]; then
    echo "missing frontend build at $ROOT_DIR/build"
    echo "run: npm install && npm run build"
    exit 1
fi

if command -v screen >/dev/null 2>&1; then
    screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
    screen -dmS "$SCREEN_NAME" bash -lc "
        cd '$ROOT_DIR' && \
        exec env PYTHONPATH='$ROOT_DIR/backend${PYTHONPATH:+:$PYTHONPATH}' \
        '$VENV_DIR/bin/python' -m uvicorn open_webui.main:app \
        --host '$HOST_VALUE' \
        --port '$PORT_VALUE' \
        --forwarded-allow-ips '*' \
        >>'$LOG_FILE' 2>&1
    "
    echo "screen:$SCREEN_NAME" >"$SESSION_FILE"
else
    nohup env \
        PYTHONPATH="$ROOT_DIR/backend${PYTHONPATH:+:$PYTHONPATH}" \
        "$VENV_DIR/bin/python" -m uvicorn open_webui.main:app \
        --host "$HOST_VALUE" \
        --port "$PORT_VALUE" \
        --forwarded-allow-ips "*" \
        >>"$LOG_FILE" 2>&1 < /dev/null &
    echo $! >"$PID_FILE"
    echo "pid:$!" >"$SESSION_FILE"
fi

for _ in $(seq 1 60); do
    if is_healthy; then
        SERVER_PID="$(pgrep -f "open_webui.main:app --host $HOST_VALUE --port $PORT_VALUE" | tail -n 1 || true)"
        if [[ -n "$SERVER_PID" ]]; then
            echo "$SERVER_PID" >"$PID_FILE"
        fi
        if [[ ! -f "$SESSION_FILE" ]]; then
            echo "pid:${SERVER_PID:-unknown}" >"$SESSION_FILE"
        fi
        echo "open-webui is ready at http://$HOST_VALUE:$PORT_VALUE"
        exit 0
    fi
    sleep 1
done

echo "open-webui did not become healthy in time"
echo "log: $LOG_FILE"
exit 1
