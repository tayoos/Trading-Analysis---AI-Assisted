#!/bin/sh
set -e
mkdir -p /data/db /data/reports /data/stocks /data/logs /data/backups 2>/dev/null || true

# Gunicorn access/error logs use the same timestamped layout via --log-config
# (see gunicorn_logging.conf). Application code logs to /data/logs/app.log.
exec gunicorn \
    --bind "0.0.0.0:${PORT:-8765}" \
    --workers 1 \
    --timeout 120 \
    --log-config gunicorn_logging.conf \
    --access-logfile /data/logs/access.log \
    --error-logfile /data/logs/error.log \
    "app:create_app()"
