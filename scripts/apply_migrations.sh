#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f .env ]]; then
  echo "Missing .env file. Copy .env.example to .env first."
  exit 1
fi

set -a
source .env
set +a

for f in db/migrations/*.sql; do
  echo "Applying $f"
  docker compose --env-file .env exec -T postgres psql \
    -U "$POSTGRES_USER" \
    -d "$POSTGRES_DB" \
    < "$f"
done

echo "Migrations applied."
