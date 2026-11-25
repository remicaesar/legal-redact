#!/bin/sh
# Run from any directory; only --reset can remove this Compose project's data.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
case "${1:-}" in
  "") [ "$#" -eq 0 ] || { echo 'Usage: ./start.sh [--reset]' >&2; exit 2; } ;;
  --reset) [ "$#" -eq 1 ] || { echo 'Usage: ./start.sh [--reset]' >&2; exit 2; } ;;
  *) echo 'Usage: ./start.sh [--reset]' >&2; exit 2 ;;
esac
if ! docker info >/dev/null 2>&1; then
  echo 'Start Docker Desktop (or your local Docker engine), then run ./start.sh again.' >&2
  exit 1
fi
docker compose version >/dev/null
if [ "${1:-}" = --reset ]; then
  echo 'RESET: deleting this install’s database, uploads, exports and credentials.'
  docker compose down --volumes
fi
docker compose up --build --wait --wait-timeout 120
printf '\nLegal Redact is ready: http://127.0.0.1:%s/login\n' "${REDACT_PORT:-5055}"
docker compose exec -T workstation python docker/bootstrap.py --credentials
# Counted, not written down: the image ships every .docx and .pdf in samples/documents.
samples=0
for sample in samples/documents/*.docx samples/documents/*.pdf; do
  if [ -f "$sample" ]; then samples=$((samples + 1)); fi
done
printf '%s fictional practice documents are ready in the Synthetic practice matter.\n' "$samples"
printf 'Stop with: docker compose stop (run it from %s)\n' "$PWD"
