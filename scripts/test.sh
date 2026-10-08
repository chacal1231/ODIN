#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -f .secrets/master.key ]]; then
  echo 'Ejecuta primero scripts/setup-local.sh' >&2
  exit 1
fi
trap 'docker compose -f compose.test.yaml down --volumes' EXIT
docker compose -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests
