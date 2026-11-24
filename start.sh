#!/usr/bin/env bash
# Startet die Transkriptions-Oberfläche und öffnet sie im Browser.
#   ./start.sh                 Standard (Port 8765)
#   ./start.sh --port 9000     anderer Port
#   ./start.sh --no-browser    ohne Browser zu öffnen
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -c "import mlx_whisper, fastapi, uvicorn" 2>/dev/null; then
  echo "Erster Start – Setup wird ausgeführt …"
  ./setup.sh --skip-test
fi

exec .venv/bin/python -m app "$@"
