#!/usr/bin/env bash
# Deploy the current checkout: backup -> build -> migrate -> restart -> health check.
# On a failed health check the previous image is restored (the database only by hand, see docs).
set -euo pipefail
scripts/backup.sh
docker image inspect otvet-app:latest > /dev/null 2>&1 && docker tag otvet-app:latest otvet-app:previous || true
docker compose build app
docker compose run --rm app alembic upgrade head
docker compose up -d app
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/healthz > /dev/null; then echo "deploy ok"; exit 0; fi
  sleep 2
done
echo "health check failed, rolling back the image" >&2
scripts/rollback.sh || true
exit 1
