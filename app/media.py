import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np

from .config import APP_DIR, MEDIA_EXTENSIONS

SAMPLE_RATE = 16000


class Cancelled(Exception):
    pass


def ffmpeg_path():
    """System ffmpeg if available, otherwise the static binary shipped with imageio-ffmpeg."""
    env = os.environ.get("FFMPEG_BINARY")
    if env and Path(env).exists():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("FFmpeg wurde nicht gefunden. Bitte setup.sh erneut ausführen.") from exc


def is_media(path):
    return Path(path).suffix.lower() in MEDIA_EXTENSIONS


def natural_key(name):
    """Sort key so that 'B3.2' comes before 'B3.10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(name))]


def scan_directory(root, recursive):
    root = Path(root)
    results = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                (d for d in dirnames if not d.startswith(".") and Path(dirpath, d).resolve() != APP_DIR),
                key=natural_key,
            )
            for f in sorted(filenames, key=natural_key):
                if not f.startswith(".") and is_media(f):
                    results.append(Path(dirpath, f))
    else:
        for p in sorted(root.iterdir(), key=lambda p: natural_key(p.name)):
            if p.is_file() and not p.name.startswith(".") and is_media(p):
                results.append(p)
    return results


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def probe_duration(path):
    """Media duration in seconds, parsed from ffmpeg's stream info (imageio-ffmpeg has no ffprobe)."""
    try:
        proc = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return None
    m = _DURATION_RE.search(proc.stderr)
    if not m:
        return None
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


def extract_audio(src, dst_wav, cancel_event=None):
    """Extract the audio track as 16 kHz mono 16-bit PCM WAV (the format Whisper expects)."""
    cmd = [
        ffmpeg_path(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(dst_wav),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        while True:
            try:
                proc.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    proc.wait()
                    raise Cancelled()
        err = proc.stderr.read().decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            if "does not contain any stream" in err or "matches no streams" in err:
                raise RuntimeError("Die Datei enthält keine Audiospur.")
            if "Invalid data found" in err or "moov atom not found" in err:
                raise RuntimeError("Datei ist beschädigt oder unvollständig – FFmpeg kann sie nicht lesen.")
            last = err.splitlines()[-1] if err else f"Exit-Code {proc.returncode}"
            raise RuntimeError(f"FFmpeg-Fehler: {last[-300:]}")
    finally:
        if proc.poll() is None:
            proc.kill()
        if proc.stderr:
            proc.stderr.close()


def load_wav(path):
    """Read a 16-bit PCM WAV written by extract_audio into a float32 array in [-1, 1]."""
    import wave

    with wave.open(str(path), "rb") as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise RuntimeError("Unerwartetes Audioformat nach der Extraktion.")
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def cleanup_tmp(tmp_dir, max_age_s=0):
    now = time.time()
    for p in Path(tmp_dir).glob("*.wav"):
        try:
            if now - p.stat().st_mtime >= max_age_s:
                p.unlink()
        except OSError:
            pass
