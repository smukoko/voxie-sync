#!/bin/bash
# The container entrypoint. Flask's built-in server is a development server --
# single-process, no request timeouts, explicitly not for a machine with a
# public IP -- so the container serves through gunicorn instead.
#
# One process with threads. Nothing needs it any more -- pairing is by
# fingerprint, not the in-memory handshake that once made -w 1 load-bearing --
# but one process keeps SQLite writes serialised and the login cache warm, and
# a sync request is a few milliseconds.
#
# --timeout must outlast a full Sync Pack upload. A book can be hundreds of MB
# over a home uplink, and gunicorn's 30s default would kill the worker mid-upload.
set -e

# Audiobookshelf items broken before the gateway repaired them itself (stale
# tracks, a stale duration) are healed once per start, so a deploy fixes them
# with no command to type. In the background, after the gateway is up: it
# never delays serving, and a failure costs only the repair.
(sleep 30 && python tools/gateway_admin.py abs-repair --wait 300) &

echo "Starting Librarian Gateway (Warehouse Mode)..."
exec gunicorn \
    --workers 1 \
    --threads 4 \
    --timeout 1800 \
    --graceful-timeout 30 \
    --bind "0.0.0.0:${PORT:-3000}" \
    --access-logfile - \
    --error-logfile - \
    --capture-output \
    librerian_gateway_v3:app
