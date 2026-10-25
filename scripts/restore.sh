#!/usr/bin/env bash
# Restore a dump into the production database. DESTRUCTIVE: replaces current data.
# Usage: scripts/restore.sh backups/otvet-XXXX.dump
set -euo pipefail
FILE="${1:?usage: restore.sh <dump file>}"
[ -f "$FILE" ] || { echo "no such file: $FILE" >&2; exit 1; }
read -r -p "This REPLACES all data in the 'otvet' database. Type 'restore' to continue: " ANSWER
[ "$ANSWER" = "restore" ] || { echo "aborted"; exit 1; }
docker compose stop app
docker compose exec -T db pg_restore -U otvet -d otvet --clean --if-exists --no-owner < "$FILE"
docker compose start app
echo "restored from $FILE"
