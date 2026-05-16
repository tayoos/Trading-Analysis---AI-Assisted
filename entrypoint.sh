#!/bin/sh
mkdir -p /data/db /data/reports /data/stocks /data/backups
exec python -m app
