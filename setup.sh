#!/usr/bin/env bash
# One-time setup: Python environment, dependencies, FFmpeg, Whisper model, self test.
#   ./setup.sh               full setup incl. model download and self test
#   ./setup.sh --skip-test   without self test
#   ./setup.sh --no-model    without downloading the model up front
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
    *) echo "Unknown option: $arg"; exit 1 ;;
  esac
done

bold() { printf "\n\033[1m%s\033[0m\n" "$*"; }
fail() { printf "\n\033[31mError: %s\033[0m\n" "$*"; exit 1; }

bold "1/5  Checking system"
[[ "$(uname -s)" == "Darwin" ]] || fail "This tool requires macOS."
[[ "$(uname -m)" == "arm64" ]] || fail "MLX requires a Mac with Apple Silicon (M1 or newer)."
echo "macOS $(sw_vers -productVersion) · $(sysctl -n machdep.cpu.brand_string) · $(( $(sysctl -n hw.memsize) / 1073741824 )) GB RAM"

bold "2/5  Setting up Python environment"
UV="$(command -v uv || true)"
[[ -z "$UV" && -x "$HOME/.local/bin/uv" ]] && UV="$HOME/.local/bin/uv"
[[ -z "$UV" && -x "$HOME/.cargo/bin/uv" ]] && UV="$HOME/.cargo/bin/uv"
if [[ -z "$UV" ]]; then
  echo "uv not found – installing it (https://astral.sh/uv) …"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$HOME/.local/bin/uv"
  [[ -x "$UV" ]] || fail "Could not install uv."
fi
if [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -c "import sys; sys.exit(sys.version_info < (3, 10))" 2>/dev/null; then
  rm -rf .venv
  "$UV" python install "$PY_VERSION"
  "$UV" venv .venv --python "$PY_VERSION"
fi
echo "Python: $(.venv/bin/python --version)"

bold "3/5  Installing dependencies"
"$UV" pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python - <<'EOF'
import mlx.core as mx, mlx_whisper, fastapi, uvicorn, huggingface_hub
print(f"mlx {mx.__version__} ({mx.default_device()}) · mlx-whisper {mlx_whisper.__version__} · fastapi {fastapi.__version__} · uvicorn {uvicorn.__version__}")
if "gpu" not in str(mx.default_device()):
    raise SystemExit("MLX cannot find a GPU.")
EOF

bold "4/5  Checking FFmpeg"
.venv/bin/python -c "from app.media import ffmpeg_path; import subprocess; p = ffmpeg_path(); print(p); print(subprocess.run([p, '-version'], capture_output=True, text=True).stdout.splitlines()[0])"

bold "5/5  Providing the Whisper model"
if [[ "$NO_MODEL" == "1" ]]; then
  echo "Skipped – the model will be downloaded on the first transcription."
else
  .venv/bin/python -c "
from app.engine import local_model_path, download_model
m = '$MODEL'
p = local_model_path(m)
print('Already available:' if p else 'Downloading (one-time, about 1.6 GB) …', p or m)
if not p:
    print(download_model(m))
"
fi

chmod +x start.sh setup.sh

if [[ "$SKIP_TEST" == "0" ]]; then
  bold "Self test"
  .venv/bin/python selftest.py
fi

bold "Done. Start with:  ./start.sh"
