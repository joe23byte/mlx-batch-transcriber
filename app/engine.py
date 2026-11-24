import gc
import logging
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import exporters, media
from .config import MODELS, RESULTS_DIR, TMP_DIR, UPLOAD_DIR
from .media import Cancelled, natural_key

log = logging.getLogger("transkription")

MODEL_RATES = {m["id"]: m["rate"] for m in MODELS}
MERGED_NAME = "Alle_Transkripte.txt"


def result_path(job_id):
    return RESULTS_DIR / f"{job_id}.json"


# ---- model handling -------------------------------------------------------

def _valid_model_dir(path):
    p = Path(path)
    return (p / "config.json").exists() and ((p / "weights.safetensors").exists() or (p / "weights.npz").exists())


def local_model_path(repo_id):
    """Path of a fully downloaded model in the Hugging Face cache, or None (never touches the network)."""
    from huggingface_hub import snapshot_download

    try:
        path = snapshot_download(repo_id=repo_id, local_files_only=True)
    except Exception:
        return None
    return path if _valid_model_dir(path) else None


def download_model(repo_id):
    from huggingface_hub import snapshot_download

    path = snapshot_download(repo_id=repo_id)
    if not _valid_model_dir(path):
        raise RuntimeError(f"Modell {repo_id} ist unvollständig heruntergeladen.")
    return path


def _clear_mlx_cache():
    import mlx.core as mx

    clear = getattr(mx, "clear_cache", None) or getattr(mx.metal, "clear_cache", None)
    if clear:
        clear()


class _ProgressBar:
    """Stand-in for tqdm inside mlx_whisper.transcribe: forwards progress and enables cancelling."""

    callback = None

    def __init__(self, total=None, **_):
        self.total = total or 1
        self.n = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def update(self, n=1):
        self.n += n
        cb = _ProgressBar.callback
        if cb:
            cb(min(1.0, self.n / self.total))

    def close(self):
        pass


class _TqdmShim:
    tqdm = _ProgressBar


# ---- engine ---------------------------------------------------------------

class Engine:
    def __init__(self, db):
        self.db = db
        self.wake = threading.Event()
        self.cancel_event = threading.Event()
        self.active = False
        self.current_id = None
        self.phase = None
        self.message = ""
        self.trans_started = None
        self.loaded_model = None
        self.model_path = None
        self.processed_this_run = 0
        self.last_merged = None
        self._thread = threading.Thread(target=self._run, name="transcriber", daemon=True)

    def start_thread(self):
        self._thread.start()

    # ---- control ------------------------------------------------------
    def start(self):
        self.db.set_status_where("queued", ["cancelled"])
        self.cancel_event.clear()
        if not self.active:
            self.processed_this_run = 0
        self.active = True
        self.wake.set()

    def cancel(self):
        self.active = False
        self.cancel_event.set()
        self.wake.set()

    def retry(self, job_id):
        job = self.db.get_job(job_id)
        if job and job["status"] in ("failed", "cancelled", "done"):
            self.db.update_job(job_id, status="queued", progress=0, phase=None, error=None)

    def retry_failed(self):
        self.db.set_status_where("queued", ["failed", "cancelled"])

    # ---- worker -------------------------------------------------------
    def _run(self):
        # All MLX work (model loading and inference) happens on this single thread.
        while True:
            self.wake.wait()
            self.wake.clear()
            while self.active:
                job = self.db.next_queued()
                if job is None:
                    self.active = False
                    if self.processed_this_run and self.db.get_settings().get("merge"):
                        try:
                            self.merge()
                        except Exception:
                            log.exception("Zusammenführen fehlgeschlagen")
                    break
                self._process(job)

    def _set_phase(self, job_id, phase, message=""):
        self.phase = phase
        self.message = message
        self.db.update_job(job_id, phase=phase)

    def _ensure_model(self, job_id, model_id):
        if self.loaded_model == model_id:
            return
        import mlx.core as mx
        tmod = sys.modules.get("mlx_whisper.transcribe")
        if tmod is None:
            import importlib
            tmod = importlib.import_module("mlx_whisper.transcribe")

        path = local_model_path(model_id)
        if path is None:
            self._set_phase(job_id, "downloading", "Modell wird einmalig heruntergeladen …")
            path = download_model(model_id)
        self._set_phase(job_id, "loading", "Modell wird geladen …")
        tmod.ModelHolder.model = None
        tmod.ModelHolder.model_path = None
        self.loaded_model = None
        gc.collect()
        _clear_mlx_cache()
        tmod.ModelHolder.get_model(path, mx.float16)
        self.loaded_model = model_id
        self.model_path = path

    def _process(self, job):
        job_id = job["id"]
        settings = self.db.get_settings()
        model_id = settings["model"]
        language = None if settings["language"] in ("", "auto", None) else settings["language"]
        prompt = (settings.get("initial_prompt") or "").strip() or None
        output_dir = settings["output_dir"]
        self.current_id = job_id
        started = time.time()
        self.db.update_job(
            job_id, status="running", progress=0, error=None, started_at=started, finished_at=None,
            model=model_id, language=language or "auto", output_dir=output_dir, elapsed=None,
        )
        wav = TMP_DIR / f"job{job_id}_{uuid.uuid4().hex[:8]}.wav"
        try:
            src = Path(job["source_path"])
            if not src.exists():
                raise RuntimeError("Quelldatei nicht gefunden.")

            self._ensure_model(job_id, model_id)
            if self.cancel_event.is_set():
                raise Cancelled()

            self._set_phase(job_id, "extracting", "Audiospur wird extrahiert …")
            media.extract_audio(src, wav, self.cancel_event)
            audio = media.load_wav(wav)
            wav.unlink(missing_ok=True)
            if audio.size < media.SAMPLE_RATE * 0.1:
                raise RuntimeError("Die Audiospur ist leer.")
            duration = audio.size / media.SAMPLE_RATE
            self.db.update_job(job_id, duration=duration)

            self._set_phase(job_id, "transcribing", "Transkription läuft …")
            self.trans_started = time.time()
            result = self._transcribe(job_id, audio, language, prompt)
            del audio

            self._set_phase(job_id, "writing", "Dateien werden geschrieben …")
            elapsed = time.time() - started
            meta = {
                "source_file": src.name,
                "source_path": str(src),
                "source_size": job["size"],
                "source_mtime": job["mtime"],
                "model": model_id,
                "language": result.get("language"),
                "language_mode": language or "auto",
                "duration_seconds": round(duration, 2),
                "processing_seconds": round(elapsed, 2),
            }
            exporters.write_outputs(
                output_dir, job["out_rel"], result, meta,
                formats=settings.get("formats") or exporters.FORMATS,
                txt_timestamps=bool(settings.get("txt_timestamps")),
                internal_path=result_path(job_id),
                replacements=settings.get("replacements"),
            )
            self.db.update_job(
                job_id, status="done", phase=None, progress=1.0, detected_language=result.get("language"),
                elapsed=elapsed, finished_at=time.time(),
            )
            self._record_rate(model_id, duration, time.time() - self.trans_started)
            self.processed_this_run += 1
            if job["is_upload"] and settings.get("delete_uploads"):
                self._delete_upload(src)
        except Cancelled:
            self.db.update_job(job_id, status="cancelled", phase=None, progress=0, elapsed=time.time() - started)
        except Exception as exc:
            log.error("Fehler bei %s:\n%s", job["name"], traceback.format_exc())
            self.db.update_job(
                job_id, status="failed", phase=None, error=str(exc) or exc.__class__.__name__,
                elapsed=time.time() - started, finished_at=time.time(),
            )
        finally:
            wav.unlink(missing_ok=True)
            _ProgressBar.callback = None
            self.current_id = None
            self.phase = None
            self.message = ""
            self.trans_started = None
            gc.collect()
            try:
                _clear_mlx_cache()
            except Exception:
                pass

    def _transcribe(self, job_id, audio, language, prompt):
        import mlx_whisper

        tmod = sys.modules["mlx_whisper.transcribe"]
        tmod.tqdm = _TqdmShim
        last = [0.0]

        def on_progress(p):
            if self.cancel_event.is_set():
                raise Cancelled()
            now = time.time()
            if now - last[0] > 0.4 or p >= 1.0:
                last[0] = now
                self.db.update_job(job_id, progress=p)

        _ProgressBar.callback = on_progress
        return mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self.model_path,
            verbose=None,
            language=language,
            task="transcribe",
            initial_prompt=prompt,
            # Not feeding previous text back in prevents hallucination cascades after silence.
            condition_on_previous_text=False,
        )

    def _delete_upload(self, src):
        try:
            src.relative_to(UPLOAD_DIR)
        except ValueError:
            return
        try:
            src.unlink(missing_ok=True)
            parent = src.parent
            while parent != UPLOAD_DIR and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        except OSError:
            pass

    # ---- statistics / ETA ---------------------------------------------
    def _record_rate(self, model_id, duration, seconds):
        if duration < 20:
            return
        rate = seconds / duration
        key = f"rate:{model_id}"
        old = self.db.get_value(key)
        self.db.set_value(key, rate if old is None else 0.6 * old + 0.4 * rate)

    def rate_for(self, model_id):
        return self.db.get_value(f"rate:{model_id}") or MODEL_RATES.get(model_id, 0.1)

    def snapshot(self):
        now = time.time()
        jobs = self.db.list_jobs()
        settings = self.db.get_settings()
        model_id = settings["model"]
        rate = self.rate_for(model_id)
        current = next((j for j in jobs if j["status"] == "running"), None)

        known = [j["duration"] for j in jobs if j["duration"]]
        avg_dur = sum(known) / len(known) if known else 600.0
        overhead = 2.0

        file_eta = None
        if current:
            dur = current["duration"] or avg_dur
            p = current["progress"] or 0.0
            current["elapsed"] = now - (current["started_at"] or now)
            if self.phase == "transcribing" and self.trans_started and p > 0.02:
                t = now - self.trans_started
                file_eta = t * (1 - p) / p
                rate = 0.5 * rate + 0.5 * (t / (p * dur)) if p < 0.2 else t / (p * dur)
            else:
                file_eta = dur * rate * (1 - p) + overhead
            current["eta"] = file_eta

        queued = [j for j in jobs if j["status"] == "queued"]
        queue_eta = (file_eta or 0.0) + sum((j["duration"] or avg_dur) * rate + overhead for j in queued)

        relevant = [j for j in jobs if j["status"] in ("done", "running", "queued")]
        total_dur = sum(j["duration"] or avg_dur for j in relevant)
        done_dur = sum(j["duration"] or avg_dur for j in relevant if j["status"] == "done")
        if current:
            done_dur += (current["duration"] or avg_dur) * (current["progress"] or 0.0)

        counts = {}
        for j in jobs:
            counts[j["status"]] = counts.get(j["status"], 0) + 1

        return {
            "worker": {
                "active": self.active,
                "phase": self.phase,
                "message": self.message,
                "current_id": self.current_id,
                "loaded_model": self.loaded_model,
            },
            "jobs": jobs,
            "counts": counts,
            "overall": {
                "progress": (done_dur / total_dur) if total_dur else 0.0,
                "eta": queue_eta if (current or queued) else None,
                "rate": rate,
                "total": len(relevant),
                "done": counts.get("done", 0),
            },
            "last_merged": self.last_merged,
        }

    # ---- merge --------------------------------------------------------
    def merge(self):
        settings = self.db.get_settings()
        entries = []
        for j in sorted(self.db.list_jobs(), key=lambda j: natural_key(j["out_rel"])):
            if j["status"] != "done":
                continue
            txt = exporters.output_paths(j["output_dir"] or settings["output_dir"], j["out_rel"])["txt"]
            try:
                entries.append((j["name"], txt.read_text(encoding="utf-8")))
            except OSError:
                continue
        if not entries:
            raise RuntimeError("Noch keine fertigen Transkripte vorhanden.")
        path = exporters.write_merged(Path(settings["output_dir"]) / MERGED_NAME, entries)
        self.last_merged = {"path": str(path), "count": len(entries), "time": time.time()}
        return self.last_merged
