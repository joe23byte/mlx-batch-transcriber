from pathlib import Path, PurePosixPath

from . import exporters, media
from .config import UPLOAD_DIR


def sanitize_relpath(rel):
    parts = [p for p in PurePosixPath(str(rel).replace("\\", "/")).parts if p not in ("", ".", "..", "/")]
    if not parts:
        raise ValueError("error.invalid_name")
    return Path(*parts)


def upload_target(rel):
    return UPLOAD_DIR / sanitize_relpath(rel)


def _fingerprint_matches(job, size, mtime):
    return job["size"] == size and abs((job["mtime"] or 0) - mtime) < 2


def is_done(db, path, size, mtime):
    job = db.get_job_by_path(str(path))
    return bool(
        job and job["status"] == "done" and _fingerprint_matches(job, size, mtime)
        and exporters.outputs_complete(job["output_dir"], job["out_rel"])
    )


def _unique_out_rel(db, out_rel, source_path):
    candidate, n = out_rel, 2
    while db.out_rel_taken(candidate, source_path):
        candidate = f"{out_rel} ({n})"
        n += 1
    return candidate


def add_file(db, path, root, is_upload=False, size=None, mtime=None):
    """Add one media file to the queue. Returns 'added', 'skipped', 'queued', 'running' or 'requeued'."""
    path = Path(path)
    if size is None or mtime is None:
        st = path.stat()
        size, mtime = st.st_size, st.st_mtime
    source = str(path)
    existing = db.get_job_by_path(source)
    if existing:
        same = _fingerprint_matches(existing, size, mtime)
        if existing["status"] == "running":
            return "running"
        if existing["status"] == "done" and same and exporters.outputs_complete(existing["output_dir"], existing["out_rel"]):
            return "skipped"
        if existing["status"] == "queued" and same:
            return "queued"
        db.update_job(
            existing["id"], status="queued", size=size, mtime=mtime, progress=0, phase=None, error=None,
            duration=existing["duration"] if same else media.probe_duration(path),
        )
        return "requeued"

    rel = path.relative_to(root) if root else Path(path.name)
    out_rel = _unique_out_rel(db, str(rel.with_suffix("")), source)
    settings = db.get_settings()
    status = "queued"
    prior = exporters.read_json(settings["output_dir"], out_rel)
    if (
        prior and prior.get("source_size") == size and abs((prior.get("source_mtime") or 0) - mtime) < 2
        and exporters.outputs_complete(settings["output_dir"], out_rel)
    ):
        status = "done"
    db.insert_job(
        source_path=source, name=str(rel), out_rel=out_rel, is_upload=1 if is_upload else 0,
        size=size, mtime=mtime, duration=media.probe_duration(path) if path.exists() else None,
        status=status, progress=1.0 if status == "done" else 0.0, output_dir=settings["output_dir"],
        detected_language=prior.get("language") if status == "done" else None,
        model=prior.get("model") if status == "done" else None,
        elapsed=prior.get("processing_seconds") if status == "done" else None,
    )
    return "skipped" if status == "done" else "added"


def add_paths(db, paths, recursive):
    """Add files and/or directories given as local paths."""
    summary = {"added": 0, "skipped": 0, "queued": 0, "running": 0, "requeued": 0, "ignored": 0}
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.exists():
            summary["ignored"] += 1
            continue
        if p.is_dir():
            p = p.resolve()
            for f in media.scan_directory(p, recursive):
                summary[add_file(db, f, p)] += 1
        elif media.is_media(p):
            p = p.resolve()
            summary[add_file(db, p, p.parent)] += 1
        else:
            summary["ignored"] += 1
    return summary
