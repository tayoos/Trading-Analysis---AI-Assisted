#!/bin/sh
set -e
mkdir -p /data/db /data/reports /data/stocks /data/logs /data/backups 2>/dev/null || true

# Obsidian: defaults apply when the vault is mounted at /obsidian (override via env if needed)
if [ -d /obsidian ]; then
  export OBSIDIAN_VAULT_DIR="${OBSIDIAN_VAULT_DIR:-/obsidian}"
  export OBSIDIAN_REPORTS_SUBDIR="${OBSIDIAN_REPORTS_SUBDIR:-10_Personal/13_Finances/Investments/AI Investment Analysis}"
  export OBSIDIAN_REPORTS_FULL_SUBDIR="${OBSIDIAN_REPORTS_FULL_SUBDIR:-Full Portfolio}"
  export OBSIDIAN_REPORTS_SINGLE_SUBDIR="${OBSIDIAN_REPORTS_SINGLE_SUBDIR:-Individual Stock}"
  export OBSIDIAN_KNOWLEDGE_ENABLED="${OBSIDIAN_KNOWLEDGE_ENABLED:-true}"
  export OBSIDIAN_KNOWLEDGE_SUBDIR="${OBSIDIAN_KNOWLEDGE_SUBDIR:-50_Knowledge/notes}"
  export OBSIDIAN_KNOWLEDGE_MOC_DIR="${OBSIDIAN_KNOWLEDGE_MOC_DIR:-50_Knowledge/_moc}"
  export OBSIDIAN_DEFAULT_MOC="${OBSIDIAN_DEFAULT_MOC:-MOC-investment-analysis}"
fi

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
