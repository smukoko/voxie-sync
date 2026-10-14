#!/bin/bash
# The image's own start: what the RunPod template's pasted start command did,
# baked in, so a template (and the public one) needs no start command at all.
#
# RunPod keeps no container log (docs/LESSONS.md), so a plain HTTP server on
# DIAG_PORT (8001) serves /tmp/diag/boot.log from the first second: a pod that
# never answers on 8000 can still say why. It holds the weights download and
# uvicorn's output; never a token.
DIAG_DIR="${DIAG_DIR:-/tmp/diag}"
DIAG_PORT="${DIAG_PORT:-8001}"
PORT="${PORT:-8000}"
VENV="${VENV:-/opt/venv}"
APP_DIR="${APP_DIR:-/opt/cosyvoice}"
WEIGHTS="${WEIGHTS:-/usr/local/bin/cosyvoice-weights}"

mkdir -p "$DIAG_DIR"
echo booting > "$DIAG_DIR/boot.log"
( "$VENV/bin/python" -m http.server "$DIAG_PORT" --directory "$DIAG_DIR" >/dev/null 2>&1 & )
{
    echo '== weights =='
    "$WEIGHTS" ensure
    echo '== starting uvicorn =='
} >> "$DIAG_DIR/boot.log" 2>&1
cd "$APP_DIR" || exit 1
exec "$VENV/bin/uvicorn" custom_api:app --host 0.0.0.0 --port "$PORT" \
    --workers "${API_WORKERS:-1}" >> "$DIAG_DIR/boot.log" 2>&1
