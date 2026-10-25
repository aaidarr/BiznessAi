#!/usr/bin/env bash
# Roll the application image back to the one that ran before the last deploy.
# Migrations are NOT reverted automatically (a down-migration can destroy data):
# if the failed release changed the schema, run `docker compose run --rm app alembic downgrade -1`
# deliberately, or restore the pre-deploy backup with scripts/restore.sh.
set -euo pipefail
docker image inspect otvet-app:previous > /dev/null 2>&1 || { echo "no previous image to roll back to" >&2; exit 1; }
docker tag otvet-app:previous otvet-app:latest
docker compose up -d --no-build app
echo "application image rolled back to otvet-app:previous"
