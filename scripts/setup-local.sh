#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -e .env || -e .secrets/master.key ]]; then
  echo 'Ya existe configuración local. No se sobrescribió ningún secreto.' >&2
  exit 1
fi
umask 077
mkdir -p .secrets
python3 - <<'PY'
import base64, secrets
from pathlib import Path
password = secrets.token_hex(32)
Path('.secrets/master.key').write_bytes(base64.urlsafe_b64encode(secrets.token_bytes(32)))
Path('.env').write_text(f'''POSTGRES_PASSWORD={password}
ODIN_DATABASE_URL=postgresql+psycopg://odin:{password}@db:5432/odin
ODIN_MASTER_KEY_FILE=/run/secrets/master.key
ODIN_ORIGIN=http://localhost:8080
ODIN_SECURE_COOKIES=false
ODIN_WEB_PORT=8080
''')
PY
# Container runs as uid 10001; parent remains 0700 and only the specific file is mounted.
chmod 444 .secrets/master.key
echo 'Configuración local creada. Acceso previsto: http://localhost:8080'
