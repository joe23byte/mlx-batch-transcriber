import json
import logging
import os
import platform
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, exporters, jobs, media
from .config import DEFAULT_SETTINGS, MEDIA_EXTENSIONS, MODEL_IDS, MODELS, STATIC_DIR, TMP_DIR, UPLOAD_DIR
from .db import Database
from .engine import MERGED_NAME, Engine, local_model_path, result_path

log = logging.getLogger("transcriber")

DIALOG_TEXT = {
    "en": {"files": "Choose video or audio files", "folder": "Choose a folder with videos", "output": "Choose output folder"},
    "de": {"files": "Video- oder Audiodateien wählen", "folder": "Ordner mit Videos wählen", "output": "Ausgabeordner wählen"},
}

db = Database()
engine = Engine(db)


@asynccontextmanager
async def lifespan(_app):
    db.recover_after_restart()
    media.cleanup_tmp(TMP_DIR)
    for p in UPLOAD_DIR.rglob("*.part"):
        p.unlink(missing_ok=True)
    engine.start_thread()
    yield
    engine.cancel()


app = FastAPI(title="MLX Batch Transcriber", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ---- models ---------------------------------------------------------------

class SettingsIn(BaseModel):
    model: Optional[str] = None
    language: Optional[str] = None
    recursive: Optional[bool] = None
    merge: Optional[bool] = None
    output_dir: Optional[str] = None
    initial_prompt: Optional[str] = None
    delete_uploads: Optional[bool] = None
    formats: Optional[List[str]] = None
    txt_timestamps: Optional[bool] = None
    replacements: Optional[str] = None


class PathsIn(BaseModel):
    paths: List[str]
    recursive: Optional[bool] = None


class UploadFileIn(BaseModel):
    path: str
    size: int
    mtime: float


class UploadListIn(BaseModel):
    files: List[UploadFileIn]


class PickIn(BaseModel):
    kind: str = "folder"
    lang: str = "en"


class OpenIn(BaseModel):
    job_id: Optional[int] = None
    target: str = "output"


# ---- meta / settings ------------------------------------------------------

@app.get("/api/ping")
def ping():
    return {"app": "mlx-batch-transcriber", "version": __version__}


@app.get("/api/meta")
def meta():
    from mlx_whisper.tokenizer import LANGUAGES

    langs = [{"code": c, "name": n.title()} for c, n in LANGUAGES.items()]
    try:
        import mlx.core as mx
        device = str(mx.default_device())
    except Exception:
        device = "unknown"
    return {
        "version": __version__,
        "models": [{**m, "downloaded": local_model_path(m["id"]) is not None} for m in MODELS],
        "languages": langs,
        "extensions": sorted(MEDIA_EXTENSIONS),
        "device": device,
        "chip": platform.processor() or platform.machine(),
        "ffmpeg": media.ffmpeg_path(),
    }


@app.get("/api/settings")
def get_settings():
    return db.get_settings()


@app.put("/api/settings")
def put_settings(body: SettingsIn):
    data = body.model_dump(exclude_none=True)
    if "model" in data and data["model"] not in MODEL_IDS:
        raise HTTPException(400, "error.unknown_model")
    if "output_dir" in data:
        out = Path(data["output_dir"]).expanduser()
        if not out.is_absolute():
            raise HTTPException(400, "error.path_not_absolute")
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(400, f"error.output_not_writable: {exc}")
        data["output_dir"] = str(out)
    if "formats" in data:
        data["formats"] = [f for f in exporters.FORMATS if f in data["formats"] or f == "txt"]
    for k, v in data.items():
        if k in DEFAULT_SETTINGS:
            db.set_value(k, v)
    return db.get_settings()


# ---- state & queue --------------------------------------------------------

@app.get("/api/state")
def state():
    return engine.snapshot()


@app.post("/api/queue/start")
def queue_start():
    engine.start()
    return {"ok": True}


@app.post("/api/queue/cancel")
def queue_cancel():
    engine.cancel()
    return {"ok": True}


@app.post("/api/jobs/retry-failed")
def retry_failed():
    engine.retry_failed()
    engine.start()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
def retry(job_id: int):
    engine.retry(job_id)
    engine.start()
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404)
    if job["status"] == "running":
        raise HTTPException(409, "error.job_running")
    db.delete_job(job_id)
    if job["is_upload"] and job["status"] != "done":
        engine._delete_upload(Path(job["source_path"]))
    return {"ok": True}


@app.post("/api/jobs/clear")
def clear_jobs(which: str = "done"):
    statuses = {"done": ["done"], "finished": ["done", "failed", "cancelled"], "all": ["done", "failed", "cancelled", "queued"]}.get(which)
    if not statuses:
        raise HTTPException(400)
    for j in db.list_jobs():
        if j["status"] in statuses:
            db.delete_job(j["id"])
            if j["is_upload"] and j["status"] != "done":
                engine._delete_upload(Path(j["source_path"]))
    return {"ok": True}


@app.post("/api/jobs/add-paths")
def add_paths(body: PathsIn):
    recursive = body.recursive if body.recursive is not None else db.get_settings()["recursive"]
    return jobs.add_paths(db, body.paths, recursive)


# ---- native macOS dialogs --------------------------------------------------

def _osascript(lines):
    args = ["osascript"]
    for line in lines:
        args += ["-e", line]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        if "-128" in proc.stderr:
            return None
        raise HTTPException(500, proc.stderr.strip() or "error.dialog_failed")
    return proc.stdout


@app.post("/api/pick")
def pick(body: PickIn):
    text = DIALOG_TEXT.get(body.lang, DIALOG_TEXT["en"])
    if body.kind == "files":
        out = _osascript([
            "activate",
            f'set fs to choose file with prompt "{text["files"]}" with multiple selections allowed',
            'set out to ""',
            "repeat with f in fs",
            "set out to out & POSIX path of f & linefeed",
            "end repeat",
            "return out",
        ])
    else:
        prompt = text["output"] if body.kind == "output" else text["folder"]
        out = _osascript(["activate", f'return POSIX path of (choose folder with prompt "{prompt}")'])
    if out is None:
        return {"paths": []}
    return {"paths": [p for p in out.splitlines() if p.strip()]}


@app.post("/api/open")
def open_in_finder(body: OpenIn):
    settings = db.get_settings()
    if body.job_id is not None:
        job = db.get_job(body.job_id)
        if not job:
            raise HTTPException(404)
        if body.target == "source":
            target = Path(job["source_path"])
        else:
            target = exporters.output_paths(job["output_dir"] or settings["output_dir"], job["out_rel"])["txt"]
        if target.exists():
            subprocess.run(["open", "-R", str(target)])
            return {"ok": True}
        raise HTTPException(404, "error.file_not_found")
    out = Path(settings["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(["open", str(out)])
    return {"ok": True}


# ---- browser uploads (drag & drop) ------------------------------------------

@app.post("/api/uploads/check")
def uploads_check(body: UploadListIn):
    result = []
    for f in body.files:
        try:
            target = jobs.upload_target(f.path)
        except ValueError:
            result.append({"path": f.path, "state": "invalid"})
            continue
        if not media.is_media(target):
            state_ = "invalid"
        elif jobs.is_done(db, target, f.size, f.mtime):
            state_ = "done"
        elif target.exists() and target.stat().st_size == f.size:
            state_ = "present"
        else:
            state_ = "needed"
        result.append({"path": f.path, "state": state_})
    return {"files": result}


@app.put("/api/uploads")
async def upload(request: Request, path: str, size: int, mtime: float):
    try:
        target = jobs.upload_target(path)
    except ValueError:
        raise HTTPException(400, "error.invalid_name")
    if not media.is_media(target):
        raise HTTPException(400, "error.unsupported_type")
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    written = 0
    try:
        with open(part, "wb") as fh:
            async for chunk in request.stream():
                fh.write(chunk)
                written += len(chunk)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    if written != size:
        part.unlink(missing_ok=True)
        raise HTTPException(400, f"error.upload_incomplete: {written}/{size} bytes")
    os.replace(part, target)
    os.utime(target, (mtime, mtime))
    return {"ok": True}


@app.post("/api/jobs/add-uploaded")
def add_uploaded(body: UploadListIn):
    summary = {"added": 0, "skipped": 0, "queued": 0, "running": 0, "requeued": 0, "ignored": 0}
    for f in body.files:
        try:
            target = jobs.upload_target(f.path)
        except ValueError:
            summary["ignored"] += 1
            continue
        if not target.exists() and not jobs.is_done(db, target, f.size, f.mtime):
            summary["ignored"] += 1
            continue
        summary[jobs.add_file(db, target, UPLOAD_DIR, is_upload=True, size=f.size, mtime=f.mtime)] += 1
    return summary


# ---- results ----------------------------------------------------------------

def _job_outputs(job_id):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404)
    return job, exporters.output_paths(job["output_dir"] or db.get_settings()["output_dir"], job["out_rel"])


def _load_doc(job_id, paths):
    for p in (result_path(job_id), paths["json"]):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


@app.get("/api/jobs/{job_id}/transcript")
def transcript(job_id: int):
    job, paths = _job_outputs(job_id)
    doc = _load_doc(job_id, paths)
    try:
        text = paths["txt"].read_text(encoding="utf-8")
    except OSError:
        text = exporters.render(doc, "txt", db.get_settings()["txt_timestamps"]) if doc else None
    if text is None:
        raise HTTPException(404, "error.transcript_not_found")
    return {"name": job["name"], "text": text, "segments": (doc or {}).get("segments", []),
            "language": (doc or {}).get("language") or job["detected_language"]}


@app.get("/api/jobs/{job_id}/download/{fmt}")
def download(job_id: int, fmt: str):
    if fmt not in exporters.FORMATS:
        raise HTTPException(400)
    _, paths = _job_outputs(job_id)
    media_type = "application/json" if fmt == "json" else "text/plain; charset=utf-8"
    if paths[fmt].exists():
        return FileResponse(paths[fmt], media_type=media_type, filename=paths[fmt].name)
    doc = _load_doc(job_id, paths)
    if not doc:
        raise HTTPException(404, "error.file_not_found")
    content = exporters.render(doc, fmt, db.get_settings()["txt_timestamps"])
    disposition = f"attachment; filename*=UTF-8''{quote(paths[fmt].name)}"
    return Response(content, media_type=media_type, headers={"Content-Disposition": disposition})


@app.post("/api/merge")
def merge():
    try:
        return engine.merge()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/merged")
def merged_download():
    info = engine.last_merged
    path = Path(info["path"]) if info else Path(db.get_settings()["output_dir"]) / MERGED_NAME
    if not path.exists():
        raise HTTPException(404, "error.no_merged")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=path.name)


# ---- frontend -----------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(FileNotFoundError)
def _fnf(_request, exc):
    return PlainTextResponse(str(exc), status_code=404)
