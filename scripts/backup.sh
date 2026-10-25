#!/usr/bin/env bash
# Logical backup of the production database. Run from the project directory (cron: daily).
# Usage: scripts/backup.sh [output_dir]   (default: ./backups)
set -euo pipefail
OUT="${1:-backups}"
mkdir -p "$OUT"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILE="$OUT/otvet-$STAMP.dump"
docker compose exec -T db pg_dump -U otvet -d otvet --format=custom --no-owner > "$FILE"
# a dump that cannot be listed is not a backup
docker compose exec -T db pg_restore --list < "$FILE" > /dev/null
chmod 600 "$FILE"
echo "backup ok: $FILE ($(wc -c < "$FILE") bytes)"
# keep the last 14 backups
ls -1t "$OUT"/otvet-*.dump | tail -n +15 | xargs -r rm --
