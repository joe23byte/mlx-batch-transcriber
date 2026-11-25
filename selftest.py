"""End-to-end self test: starts the server in an isolated temp directory and checks adding files,
transcription, exports, skipping, cancel/resume, failures/retry, uploads, merging, recovery after a
hard restart and the UI translations.

    .venv/bin/python selftest.py [--model mlx-community/whisper-tiny-mlx] [--keep]
"""
import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.engine import MERGED_NAME  # noqa: E402
from app.media import ffmpeg_path  # noqa: E402

APP_ID = "mlx-batch-transcriber"
SPEECH = {
    "de": (
        "Guten Tag und herzlich willkommen. Dies ist ein Funktionstest für die lokale Transkription. "
        "Die Verarbeitung erfolgt vollständig auf dem Mac, ohne Cloud und ohne laufende Kosten.",
        ("funktionstest", "transkription"),
    ),
    "en": (
        "Hello and welcome. This is a self test for the local transcription tool. "
        "Everything runs entirely on the Mac, without any cloud and without running costs.",
        ("test", "transcription"),
    ),
}


class Fail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)
    print(f"  ✓ {msg}")


def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def make_media(d):
    """Creates test media with macOS text-to-speech. Prefers a German voice to test auto-detection
    of a non-English language; falls back to English if none is installed."""
    voices = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout.splitlines()
    german = next((m.group(1) for v in voices if (m := re.match(r"(.+?)\s+de_DE\s", v))), None)
    lang = "de" if german else "en"
    text, keywords = SPEECH[lang]
    ff = ffmpeg_path()
    aiff = d / "speech.aiff"
    run(["say", *(["-v", german] if german else []), "-o", str(aiff), text])
    media = d / "media"
    (media / "subfolder").mkdir(parents=True)
    run([ff, "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=10", "-i", str(aiff), "-shortest",
         "-c:v", "mpeg4", "-c:a", "aac", str(media / "Testvideo.mp4")])
    run([ff, "-y", "-i", str(aiff), "-c:a", "aac", str(media / "subfolder" / "Testaudio.m4a")])
    run([ff, "-y", "-stream_loop", "29", "-i", str(aiff), "-ar", "16000", "-ac", "1", str(d / "Long.wav")])
    (media / "broken.mov").write_bytes(os.urandom(4096))
    (media / "notes.txt").write_text("not a media file")
    return media, lang, keywords


def check_translations():
    src = (ROOT / "app" / "static" / "i18n.js").read_text(encoding="utf-8")
    blocks = dict(re.findall(r"\n  (en|de): \{(.*?)\n  \},", src, re.S))
    keys = {lang: set(re.findall(r'^\s*"([\w.]+)":', body, re.M)) for lang, body in blocks.items()}
    check(set(keys) == {"en", "de"} and keys["en"] == keys["de"], f"UI translations complete ({len(keys.get('en', ()))} keys in EN and DE)")
    used = set()
    for py in (ROOT / "app").glob("*.py"):
        used |= set(re.findall(r'"(error\.[a-z_]+)', py.read_text(encoding="utf-8")))
    missing = used - keys["en"]
    check(not missing, f"All server error keys are translated ({len(used)} keys){' – missing: ' + ', '.join(sorted(missing)) if missing else ''}")


class Server:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.proc = None

    def start(self):
        env = {**os.environ, "TRANSCRIBER_DATA_DIR": str(self.data_dir)}
        self.log = open(self.data_dir / "server.log", "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app", "--port", str(self.port), "--no-browser"],
            cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT,
        )
        for _ in range(240):
            try:
                if self.get("/api/ping")["app"] == APP_ID:
                    return
            except Exception:
                time.sleep(0.25)
        raise Fail("Server does not start")

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.kill()

    def req(self, method, path, body=None, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = dict(headers or {})
        if body is not None:
            h["Content-Type"] = "application/json"
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=h)
        with urllib.request.urlopen(r, timeout=120) as resp:
            content = resp.read()
            ctype = resp.headers.get("Content-Type", "")
        return json.loads(content) if "json" in ctype and not path.endswith("/json") else content

    def get(self, path):
        return self.req("GET", path)

    def post(self, path, body=None):
        return self.req("POST", path, body=body if body is not None else {})

    def state(self):
        return self.get("/api/state")

    def wait_idle(self, timeout=900):
        t0 = time.time()
        while time.time() - t0 < timeout:
            s = self.state()
            if not s["worker"]["active"] and not s["counts"].get("running"):
                return s
            time.sleep(0.3)
        raise Fail("Processing timed out")

    def job(self, name_part):
        return next(j for j in self.state()["jobs"] if name_part in j["name"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="mlx-community/whisper-large-v3-turbo")
    ap.add_argument("--keep", action="store_true", help="keep the temp directory")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="transcriber-test-"))
    data, out = tmp / "data", tmp / "output"
    data.mkdir()
    srv = Server(data)
    ok = False
    try:
        print("Checking translations …")
        check_translations()

        print("Creating test media …")
        media, lang, keywords = make_media(tmp)

        print("Starting server …")
        srv.start()
        check(b"Transcriber" in srv.get("/") and b"i18n.js" in srv.get("/"), "Web UI is served")
        meta = srv.get("/api/meta")
        check("gpu" in meta["device"], f"MLX uses the GPU ({meta['device']})")
        codes = {l["code"] for l in meta["languages"]}
        check(len(codes) > 90 and {"en", "de"} <= codes, "Language list available")
        srv.req("PUT", "/api/settings", body={"output_dir": str(out), "model": args.model, "language": "auto", "merge": True})

        print("Adding files …")
        s = srv.post("/api/jobs/add-paths", {"paths": [str(media)], "recursive": False})
        check(s["added"] == 2, "Non-recursive: 2 media files (TXT ignored, subfolder skipped)")
        s = srv.post("/api/jobs/add-paths", {"paths": [str(media)], "recursive": True})
        check(s["added"] == 1 and s["queued"] == 2, "Recursive: file in subfolder found as well")
        check(srv.job("Testvideo")["duration"] > 5, "Media duration detected via FFmpeg")

        print("Transcribing (incl. model loading) …")
        t0 = time.time()
        srv.post("/api/queue/start")
        stop_hammer = threading.Event()

        def hammer():
            while not stop_hammer.is_set():
                try:
                    srv.state()
                    srv.get("/api/settings")
                except Exception:
                    pass

        hammers = [threading.Thread(target=hammer, daemon=True) for _ in range(4)]
        for h in hammers:
            h.start()
        st = srv.wait_idle()
        stop_hammer.set()
        print(f"    Duration: {time.time() - t0:.1f} s")
        check(st["worker"]["loaded_model"] == args.model, "Model loaded and kept in memory")
        v, a, k = srv.job("Testvideo"), srv.job("Testaudio"), srv.job("broken")
        check(v["status"] == "done" and a["status"] == "done", "MP4 and M4A transcribed")
        check(v["detected_language"] == lang, f"Language auto-detected as '{lang}'")
        check(k["status"] == "failed" and k["error"] == "error.corrupt", "Broken file marked as failed (error.corrupt)")

        for f in ("Testvideo.txt", "Testvideo.srt", "Testvideo.vtt", "Testvideo.json", "subfolder/Testaudio.txt"):
            check((out / f).exists(), f"Export exists: {f}")
        txt = (out / "Testvideo.txt").read_text().lower()
        check(all(kw in txt for kw in keywords), "Transcript contains the spoken text")
        check(txt.startswith("[00:00] "), "TXT has minute timestamps")
        srt = (out / "Testvideo.srt").read_text()
        check(srt.startswith("1\n00:00:0") and " --> " in srt, "SRT format correct")
        check((out / "Testvideo.vtt").read_text().startswith("WEBVTT"), "VTT format correct")
        doc = json.loads((out / "Testvideo.json").read_text())
        check(doc["segments"] and {"start", "end", "text"} <= set(doc["segments"][0]), "JSON with timestamps")
        check(not list((data / "tmp").glob("*.wav")), "Temporary audio files deleted")
        merged = out / MERGED_NAME
        check(merged.exists() and "\nTestvideo.mp4\n" in merged.read_text(), "Merged file uses file names as headings")

        t = srv.get(f"/api/jobs/{v['id']}/transcript")
        check(keywords[0] in t["text"].lower(), "Preview API returns the transcript")
        check(b"-->" in srv.get(f"/api/jobs/{v['id']}/download/srt"), "Download API returns SRT")

        print("Skipping / retrying …")
        s = srv.post("/api/jobs/add-paths", {"paths": [str(media)], "recursive": True})
        check(s["skipped"] == 2, "Already transcribed files are skipped")
        srv.post("/api/jobs/retry-failed")
        srv.wait_idle()
        check(srv.job("broken")["status"] == "failed", "Failed file was retried")
        srv.req("DELETE", f"/api/jobs/{srv.job('broken')['id']}")

        print("Cancel and resume …")
        srv.req("PUT", "/api/settings", body={"formats": ["txt"]})
        srv.post("/api/jobs/add-paths", {"paths": [str(tmp / "Long.wav")]})
        srv.post("/api/queue/start")
        t0 = time.time()
        while time.time() - t0 < 120:
            j = srv.job("Long.wav")
            if j["status"] == "running" and j["phase"] == "transcribing" and j["progress"] > 0:
                break
            time.sleep(0.05)
        srv.post("/api/queue/cancel")
        srv.wait_idle()
        check(srv.job("Long.wav")["status"] == "cancelled", "Running transcription cancelled")
        srv.post("/api/queue/start")
        srv.wait_idle()
        check(srv.job("Long.wav")["status"] == "done", "Finished after resuming")
        long_txt = (out / "Long.txt").read_text()
        check(not (out / "Long.srt").exists() and not (out / "Long.json").exists(), "TXT only: no other files in output folder")
        check(long_txt.count("\n\n[") >= 3 and "[01:" in long_txt, "Long TXT split into minute paragraphs")
        lid = srv.job("Long.wav")["id"]
        check(b"-->" in srv.get(f"/api/jobs/{lid}/download/srt"), "SRT still downloadable (generated on demand)")

        print("Upload (drag & drop) …")
        src = media / "Testvideo.mp4"
        raw = src.read_bytes()
        info = {"path": "Upload/Uploaded.mp4", "size": len(raw), "mtime": src.stat().st_mtime}
        c = srv.post("/api/uploads/check", {"files": [info]})
        check(c["files"][0]["state"] == "needed", "Upload check detects a new file")
        q = urllib.parse.urlencode(info)
        srv.req("PUT", f"/api/uploads?{q}", raw=raw, headers={"Content-Type": "application/octet-stream"})
        s = srv.post("/api/jobs/add-uploaded", {"files": [info]})
        check(s["added"] == 1, "Uploaded file queued")
        srv.post("/api/queue/start")
        srv.wait_idle()
        check(srv.job("Uploaded")["status"] == "done", "Uploaded file transcribed")
        check(not (data / "uploads" / "Upload" / "Uploaded.mp4").exists(), "Upload copy deleted after success")
        c = srv.post("/api/uploads/check", {"files": [info]})
        check(c["files"][0]["state"] == "done", "Re-upload recognized as already done")

        print("Restart during processing …")
        shutil.copy(tmp / "Long.wav", tmp / "Long2.wav")
        srv.post("/api/jobs/add-paths", {"paths": [str(tmp / "Long2.wav")]})
        srv.post("/api/queue/start")
        t0 = time.time()
        while srv.job("Long2")["status"] != "running" and time.time() - t0 < 60:
            time.sleep(0.05)
        srv.kill()
        srv.start()
        j = srv.job("Long2")
        check(j["status"] == "queued", "Back in the queue after a hard restart")
        check(srv.job("Testvideo")["status"] == "done", "Finished jobs survive a restart")
        srv.post("/api/queue/start")
        srv.wait_idle()
        check(srv.job("Long2")["status"] == "done", "Resumed and finished after restart")
        srv.log.flush()
        log_text = (data / "server.log").read_text(errors="replace")
        check("Exception in ASGI application" not in log_text, "No API errors in the server log (incl. concurrent polling)")
        ok = True
    except Fail as exc:
        print(f"\n  ✗ {exc}")
    finally:
        srv.stop()
        if not ok:
            log = data / "server.log"
            if log.exists():
                print("\n--- Server log ---\n" + log.read_text(errors="replace")[-4000:])
        if args.keep or not ok:
            print(f"\nTest data: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    print("\nSelf test passed ✓" if ok else "\nSelf test FAILED ✗")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
