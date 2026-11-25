#!/usr/bin/env bash
# Starts the transcriber and opens it in the browser.
#   ./start.sh                 default (port 8765)
#   ./start.sh --port 9000     different port
#   ./start.sh --no-browser    do not open the browser
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -c "import mlx_whisper, fastapi, uvicorn" 2>/dev/null; then
  echo "First start – running setup …"
  ./setup.sh --skip-test
fi

exec .venv/bin/python -m app "$@"
