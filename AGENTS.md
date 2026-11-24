# AGENTS.md

Local batch transcription app for macOS on Apple Silicon (MLX Whisper + FastAPI + vanilla JS UI).
The user-facing UI and README are German; keep new UI strings German.

## Setup and run

- Requirements: macOS on Apple Silicon (arm64). Intel Macs and Linux are not supported (MLX).
- `./setup.sh` creates `.venv/` with Python 3.12 via `uv` (installed automatically if missing), installs
  `requirements.txt`, checks FFmpeg, downloads the default model (~1.6 GB) and runs `selftest.py`.
  Use `./setup.sh --skip-test` for a faster setup.
- `./start.sh` starts the server on `http://127.0.0.1:8765` and opens the browser. Use `--no-browser` when
  running headless and `--port N` for another port. A second launch detects the running instance.
- No Homebrew or system FFmpeg needed: `imageio-ffmpeg` ships an arm64 binary (system FFmpeg wins if present).

## Tests

- `.venv/bin/python selftest.py` runs the end-to-end test (~3 minutes). It generates speech with macOS
  `say`, starts an isolated server with `TRANSKRIPTION_DATA_DIR` pointing to a temp dir and exercises the
  HTTP API. It never touches the real `data/` directory. Run it after any change to `app/`.

## Architecture notes

- `app/engine.py`: a single worker thread owns all MLX work (model loading and inference). Keep it that way;
  MLX streams are thread-bound. Progress and cancellation hook into `mlx_whisper.transcribe` by replacing its
  `tqdm` reference with `_TqdmShim`.
- `app/db.py`: one SQLite connection shared across threads. Always fetch rows inside the lock (`_all`/`_one`).
- `app/exporters.py`: TXT is written last and marks a job as complete. The full result is also stored in
  `data/results/<job_id>.json` so every format stays downloadable regardless of the selected formats.
- Models are resolved with `snapshot_download(local_files_only=True)` first so the app works offline.
- `data/` (state, uploads, temp audio) and `transkripte/` (default output) are git-ignored runtime folders.
