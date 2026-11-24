#!/usr/bin/env bash
# Einmaliges Setup: Python-Umgebung, Abhängigkeiten, FFmpeg, Whisper-Modell, Funktionstest.
#   ./setup.sh               komplettes Setup inkl. Modell-Download und Funktionstest
#   ./setup.sh --skip-test   ohne Funktionstest
#   ./setup.sh --no-model    ohne Vorab-Download des Modells
set -euo pipefail
cd "$(dirname "$0")"

PY_VERSION="3.12"
MODEL="mlx-community/whisper-large-v3-turbo"
SKIP_TEST=0
NO_MODEL=0
for arg in "$@"; do
  case "$arg" in
    --skip-test) SKIP_TEST=1 ;;
    --no-model) NO_MODEL=1 ;;
    *) echo "Unbekannte Option: $arg"; exit 1 ;;
  esac
done

bold() { printf "\n\033[1m%s\033[0m\n" "$*"; }
fail() { printf "\n\033[31mFehler: %s\033[0m\n" "$*"; exit 1; }

bold "1/5  System prüfen"
[[ "$(uname -s)" == "Darwin" ]] || fail "Dieses Tool benötigt macOS."
[[ "$(uname -m)" == "arm64" ]] || fail "MLX benötigt einen Mac mit Apple Silicon (M1 oder neuer)."
echo "macOS $(sw_vers -productVersion) · $(sysctl -n machdep.cpu.brand_string) · $(( $(sysctl -n hw.memsize) / 1073741824 )) GB RAM"

bold "2/5  Python-Umgebung einrichten"
UV="$(command -v uv || true)"
[[ -z "$UV" && -x "$HOME/.local/bin/uv" ]] && UV="$HOME/.local/bin/uv"
[[ -z "$UV" && -x "$HOME/.cargo/bin/uv" ]] && UV="$HOME/.cargo/bin/uv"
if [[ -z "$UV" ]]; then
  echo "uv nicht gefunden – wird installiert (https://astral.sh/uv) …"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$HOME/.local/bin/uv"
  [[ -x "$UV" ]] || fail "uv konnte nicht installiert werden."
fi
if [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -c "import sys; sys.exit(sys.version_info < (3, 10))" 2>/dev/null; then
  rm -rf .venv
  "$UV" python install "$PY_VERSION"
  "$UV" venv .venv --python "$PY_VERSION"
fi
echo "Python: $(.venv/bin/python --version)"

bold "3/5  Abhängigkeiten installieren"
"$UV" pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python - <<'EOF'
import mlx.core as mx, mlx_whisper, fastapi, uvicorn, huggingface_hub
print(f"mlx {mx.__version__} ({mx.default_device()}) · mlx-whisper {mlx_whisper.__version__} · fastapi {fastapi.__version__} · uvicorn {uvicorn.__version__}")
if "gpu" not in str(mx.default_device()):
    raise SystemExit("MLX findet keine GPU.")
EOF

bold "4/5  FFmpeg prüfen"
.venv/bin/python -c "from app.media import ffmpeg_path; import subprocess; p = ffmpeg_path(); print(p); print(subprocess.run([p, '-version'], capture_output=True, text=True).stdout.splitlines()[0])"

bold "5/5  Whisper-Modell bereitstellen"
if [[ "$NO_MODEL" == "1" ]]; then
  echo "Übersprungen – das Modell wird bei der ersten Transkription geladen."
else
  .venv/bin/python -c "
from app.engine import local_model_path, download_model
m = '$MODEL'
p = local_model_path(m)
print('Bereits vorhanden:' if p else 'Lade herunter (einmalig, ca. 1,6 GB) …', p or m)
if not p:
    print(download_model(m))
"
fi

chmod +x start.sh setup.sh

if [[ "$SKIP_TEST" == "0" ]]; then
  bold "Funktionstest"
  .venv/bin/python selftest.py
fi

bold "Fertig. Starten mit:  ./start.sh"
