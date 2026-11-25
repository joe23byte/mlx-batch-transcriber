import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TRANSCRIBER_DATA_DIR") or APP_DIR / "data")
UPLOAD_DIR = DATA_DIR / "uploads"
TMP_DIR = DATA_DIR / "tmp"
RESULTS_DIR = DATA_DIR / "results"
DB_PATH = DATA_DIR / "state.db"
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_OUTPUT_DIR = APP_DIR / "transcripts"

for d in (DATA_DIR, UPLOAD_DIR, TMP_DIR, RESULTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".m4a"}

# Rough processing time per second of audio, used for ETA until real measurements exist.
MODELS = [
    {"id": "mlx-community/whisper-large-v3-turbo", "name": "Large v3 Turbo", "tag": "recommended", "size_mb": 1600, "rate": 0.05},
    {"id": "mlx-community/whisper-large-v3-mlx", "name": "Large v3", "tag": "accurate", "size_mb": 3100, "rate": 0.12},
    {"id": "mlx-community/whisper-medium-mlx", "name": "Medium", "tag": None, "size_mb": 1500, "rate": 0.07},
    {"id": "mlx-community/whisper-small-mlx", "name": "Small", "tag": None, "size_mb": 480, "rate": 0.03},
    {"id": "mlx-community/whisper-base-mlx", "name": "Base", "tag": None, "size_mb": 150, "rate": 0.015},
    {"id": "mlx-community/whisper-tiny-mlx", "name": "Tiny", "tag": "fast", "size_mb": 75, "rate": 0.01},
]
DEFAULT_MODEL = MODELS[0]["id"]
MODEL_IDS = {m["id"] for m in MODELS}

DEFAULT_SETTINGS = {
    "model": DEFAULT_MODEL,
    "language": "auto",
    "recursive": True,
    "merge": False,
    "output_dir": str(DEFAULT_OUTPUT_DIR),
    "initial_prompt": "",
    "delete_uploads": True,
    "formats": ["txt", "srt", "vtt", "json"],
    "txt_timestamps": True,
    "replacements": "",
}
