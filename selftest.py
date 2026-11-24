"""End-to-end Funktionstest: startet den Server in einem isolierten Temp-Verzeichnis und
prüft Hinzufügen, Transkription, Exporte, Überspringen, Abbrechen/Fortsetzen, Fehler/Wiederholen,
Upload, Zusammenführen und Wiederaufnahme nach einem Neustart.

    .venv/bin/python selftest.py [--model mlx-community/whisper-tiny-mlx]
"""
import argparse
import json
import os
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

from app.media import ffmpeg_path  # noqa: E402

SPEECH = (
    "Guten Tag und herzlich willkommen. Dies ist ein Funktionstest für die lokale Transkription. "
    "Die Verarbeitung erfolgt vollständig auf dem Mac, ohne Cloud und ohne laufende Kosten."
)


class Fail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)
    print(f"  ✓ {msg}")


def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def make_media(d):
    ff = ffmpeg_path()
    aiff = d / "speech.aiff"
    voice = []
    voices = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    for name in ("Anna", "Petra", "Markus", "Yannick"):
        if any(line.startswith(name + " ") for line in voices.splitlines()):
            voice = ["-v", name]
            break
    run(["say", *voice, "-o", str(aiff), SPEECH])
    media = d / "medien"
    (media / "unterordner").mkdir(parents=True)
    run([ff, "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=10", "-i", str(aiff), "-shortest",
         "-c:v", "mpeg4", "-c:a", "aac", str(media / "Testvideo.mp4")])
    run([ff, "-y", "-i", str(aiff), "-c:a", "aac", str(media / "unterordner" / "Testaudio.m4a")])
    run([ff, "-y", "-stream_loop", "29", "-i", str(aiff), "-ar", "16000", "-ac", "1", str(d / "Lang.wav")])
    (media / "kaputt.mov").write_bytes(os.urandom(4096))
    (media / "notiz.txt").write_text("keine Mediendatei")
    return media


class Server:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.proc = None

    def start(self):
        env = {**os.environ, "TRANSKRIPTION_DATA_DIR": str(self.data_dir)}
        self.log = open(self.data_dir / "server.log", "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app", "--port", str(self.port), "--no-browser"],
            cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT,
        )
        for _ in range(240):
            try:
                if self.get("/api/ping")["app"] == "transkription":
                    return
            except Exception:
                time.sleep(0.25)
        raise Fail("Server startet nicht")

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
        raise Fail("Zeitüberschreitung bei der Verarbeitung")

    def job(self, name_part):
        return next(j for j in self.state()["jobs"] if name_part in j["name"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="mlx-community/whisper-large-v3-turbo")
    ap.add_argument("--keep", action="store_true", help="Temp-Verzeichnis nicht löschen")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="transkription-test-"))
    data, out = tmp / "data", tmp / "ausgabe"
    data.mkdir()
    srv = Server(data)
    ok = False
    try:
        print("Testmedien erzeugen …")
        media = make_media(tmp)

        print("Server starten …")
        srv.start()
        check(b"Transkription" in srv.get("/"), "Weboberfläche wird ausgeliefert")
        meta = srv.get("/api/meta")
        check("gpu" in meta["device"], f"MLX nutzt die GPU ({meta['device']})")
        check(len(meta["languages"]) > 90 and meta["languages"][0]["code"] == "de", "Sprachliste verfügbar")
        srv.req("PUT", "/api/settings", body={"output_dir": str(out), "model": args.model, "language": "auto", "merge": True})

        print("Dateien hinzufügen …")
        s = srv.post("/api/jobs/add-paths", {"paths": [str(media)], "recursive": False})
        check(s["added"] == 2, "Nicht-rekursiv: 2 Mediendateien (TXT ignoriert, Unterordner ausgelassen)")
        s = srv.post("/api/jobs/add-paths", {"paths": [str(media)], "recursive": True})
        check(s["added"] == 1 and s["queued"] == 2, "Rekursiv: Datei aus Unterordner zusätzlich gefunden")
        check(srv.job("Testvideo")["duration"] > 5, "Mediendauer per FFmpeg ermittelt")

        print("Transkription (inkl. Modell laden) …")
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
        print(f"    Dauer: {time.time() - t0:.1f} s")
        check(st["worker"]["loaded_model"] == args.model, "Modell geladen und im Speicher gehalten")
        v, a, k = srv.job("Testvideo"), srv.job("Testaudio"), srv.job("kaputt")
        check(v["status"] == "done" and a["status"] == "done", "MP4 und M4A erfolgreich transkribiert")
        check(v["detected_language"] == "de", "Sprache automatisch als Deutsch erkannt")
        check(k["status"] == "failed" and k["error"], f"Defekte Datei als fehlgeschlagen markiert ({k['error'][:60]} …)")

        base = out
        for f in ("Testvideo.txt", "Testvideo.srt", "Testvideo.vtt", "Testvideo.json", "unterordner/Testaudio.txt"):
            check((base / f).exists(), f"Export vorhanden: {f}")
        txt = (base / "Testvideo.txt").read_text().lower()
        check("funktionstest" in txt and "transkription" in txt, "Transkript enthält den gesprochenen Text")
        check(txt.startswith("[00:00] "), "TXT mit Minuten-Zeitmarke")
        srt = (base / "Testvideo.srt").read_text()
        check(srt.startswith("1\n00:00:0") and " --> " in srt, "SRT-Format korrekt")
        check((base / "Testvideo.vtt").read_text().startswith("WEBVTT"), "VTT-Format korrekt")
        doc = json.loads((base / "Testvideo.json").read_text())
        check(doc["segments"] and {"start", "end", "text"} <= set(doc["segments"][0]), "JSON mit Zeitstempeln")
        check(not list((data / "tmp").glob("*.wav")), "Temporäre Audiodateien gelöscht")
        merged = out / "Alle_Transkripte.txt"
        check(merged.exists() and "\nTestvideo.mp4\n" in merged.read_text(), "Zusammenführung mit Dateinamen als Überschrift")

        t = srv.get(f"/api/jobs/{v['id']}/transcript")
        check("Funktionstest".lower() in t["text"].lower(), "Vorschau-API liefert Transkript")
        check(b"-->" in srv.get(f"/api/jobs/{v['id']}/download/srt"), "Download-API liefert SRT")

        print("Überspringen / Wiederholen …")
        s = srv.post("/api/jobs/add-paths", {"paths": [str(media)], "recursive": True})
        check(s["skipped"] == 2, "Bereits transkribierte Dateien werden übersprungen")
        srv.post("/api/jobs/retry-failed")
        srv.wait_idle()
        check(srv.job("kaputt")["status"] == "failed", "Fehlgeschlagene Datei erneut versucht")
        srv.req("DELETE", f"/api/jobs/{srv.job('kaputt')['id']}")

        print("Abbrechen und Fortsetzen …")
        srv.req("PUT", "/api/settings", body={"formats": ["txt"]})
        srv.post("/api/jobs/add-paths", {"paths": [str(tmp / "Lang.wav")]})
        srv.post("/api/queue/start")
        t0 = time.time()
        while time.time() - t0 < 120:
            j = srv.job("Lang.wav")
            if j["status"] == "running" and j["phase"] == "transcribing" and j["progress"] > 0:
                break
            time.sleep(0.05)
        srv.post("/api/queue/cancel")
        srv.wait_idle()
        check(srv.job("Lang.wav")["status"] == "cancelled", "Laufende Transkription abgebrochen")
        srv.post("/api/queue/start")
        srv.wait_idle()
        check(srv.job("Lang.wav")["status"] == "done", "Nach Fortsetzen fertig transkribiert")
        lang_txt = (out / "Lang.txt").read_text()
        check(not (out / "Lang.srt").exists() and not (out / "Lang.json").exists(), "Nur TXT: keine weiteren Dateien im Ausgabeordner")
        check(lang_txt.count("\n\n[") >= 3 and "[01:" in lang_txt, "Langes TXT in Minuten-Absätze gegliedert")
        lid = srv.job("Lang.wav")["id"]
        check(b"-->" in srv.get(f"/api/jobs/{lid}/download/srt"), "SRT trotzdem herunterladbar (bei Bedarf erzeugt)")

        print("Upload (Drag & Drop) …")
        src = media / "Testvideo.mp4"
        raw = src.read_bytes()
        info = {"path": "Upload/Hochgeladen.mp4", "size": len(raw), "mtime": src.stat().st_mtime}
        c = srv.post("/api/uploads/check", {"files": [info]})
        check(c["files"][0]["state"] == "needed", "Upload-Prüfung erkennt neue Datei")
        q = urllib.parse.urlencode(info)
        srv.req("PUT", f"/api/uploads?{q}", raw=raw, headers={"Content-Type": "application/octet-stream"})
        s = srv.post("/api/jobs/add-uploaded", {"files": [info]})
        check(s["added"] == 1, "Hochgeladene Datei eingereiht")
        srv.post("/api/queue/start")
        srv.wait_idle()
        check(srv.job("Hochgeladen")["status"] == "done", "Hochgeladene Datei transkribiert")
        check(not (data / "uploads" / "Upload" / "Hochgeladen.mp4").exists(), "Upload-Kopie nach Erfolg gelöscht")
        c = srv.post("/api/uploads/check", {"files": [info]})
        check(c["files"][0]["state"] == "done", "Erneuter Upload wird als bereits erledigt erkannt")

        print("Neustart während der Verarbeitung …")
        shutil.copy(tmp / "Lang.wav", tmp / "Lang2.wav")
        srv.post("/api/jobs/add-paths", {"paths": [str(tmp / "Lang2.wav")]})
        srv.post("/api/queue/start")
        t0 = time.time()
        while srv.job("Lang2")["status"] != "running" and time.time() - t0 < 60:
            time.sleep(0.05)
        srv.kill()
        srv.start()
        j = srv.job("Lang2")
        check(j["status"] == "queued", "Nach hartem Neustart wieder in der Warteschlange")
        check(srv.job("Testvideo")["status"] == "done", "Fertige Jobs bleiben nach Neustart erhalten")
        srv.post("/api/queue/start")
        srv.wait_idle()
        check(srv.job("Lang2")["status"] == "done", "Nach Neustart fortgesetzt und fertig")
        srv.log.flush()
        log_text = (data / "server.log").read_text(errors="replace")
        check("Exception in ASGI application" not in log_text, "Keine API-Fehler im Server-Log (auch bei parallelem Abfragen)")
        ok = True
    except Fail as exc:
        print(f"\n  ✗ {exc}")
    finally:
        srv.stop()
        if not ok:
            log = data / "server.log"
            if log.exists():
                print("\n--- Server-Log ---\n" + log.read_text(errors="replace")[-4000:])
        if args.keep or not ok:
            print(f"\nTestdaten: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    print("\nFunktionstest bestanden ✓" if ok else "\nFunktionstest FEHLGESCHLAGEN ✗")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
