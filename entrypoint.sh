#!/bin/sh
mkdir -p /data/db /data/reports /data/stocks
exec gunicorn \
    --bind "0.0.0.0:${PORT:-8765}" \
    --workers 1 \
    --timeout 120 \
    --log-level info \
    "app:create_app()"
