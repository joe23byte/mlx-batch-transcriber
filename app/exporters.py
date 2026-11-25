import json
import os
import re
import time
from pathlib import Path

FORMATS = ("txt", "srt", "vtt", "json")


def _ts(seconds, sep):
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


# Phrases Whisper is known to invent on silence or music (learned from subtitled TV material).
_HALLUCINATION_RE = re.compile(
    r"untertitel(ung)?\s+(des|der|im auftrag des|von)\b|amara\.org|zdf für funk|"
    r"copyright\s.*\b(wdr|swr|zdf|ard|ndr)\b|subtitles by|thanks for watching",
    re.IGNORECASE,
)
_FILLERS = {
    "vielen dank", "danke", "danke schön", "dankeschön", "tschüss", "auf wiedersehen",
    "auf wiederhören", "bis zum nächsten mal", "bis bald", "untertitel", "thank you",
    "thanks", "thank you very much", "bye", "goodbye", "see you next time",
}


def _norm(text):
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


def _is_hallucination(seg, text, prev_norm):
    if not re.search(r"\w", text):
        return True
    if _HALLUCINATION_RE.search(text):
        return True
    if seg.get("avg_logprob") is not None and seg["avg_logprob"] < -1.5:
        return True
    norm = _norm(text)
    if norm in _FILLERS and (float(seg["end"]) - float(seg["start"]) > 6 or norm == prev_norm):
        return True
    return False


def parse_replacements(spec):
    """'alt => neu' per line; returns a list of (compiled regex, replacement)."""
    rules = []
    for line in (spec or "").splitlines():
        for sep in ("=>", "->", "→"):
            if sep in line:
                old, new = (p.strip() for p in line.split(sep, 1))
                if old:
                    rules.append((re.compile(rf"(?<!\w){re.escape(old)}(?!\w)", re.IGNORECASE), new))
                break
    return rules


def clean_segments(result, replacements=None):
    rules = parse_replacements(replacements)
    segs, prev_norm = [], None
    for s in result.get("segments", []):
        text = (s.get("text") or "").strip()
        if not text or _is_hallucination(s, text, prev_norm):
            continue
        for rx, new in rules:
            text = rx.sub(new, text)
        prev_norm = _norm(text)
        segs.append({"id": len(segs), "start": round(float(s["start"]), 3), "end": round(float(s["end"]), 3), "text": text})
    return segs


def _clock(seconds):
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def to_txt(segments, timestamps=False):
    if not timestamps:
        return "\n".join(s["text"] for s in segments) + "\n"
    # One paragraph per minute, prefixed with the start time of its first segment.
    paragraphs, current, mark = [], [], None
    for s in segments:
        if mark is None or int(s["start"] // 60) > int(mark // 60):
            if current:
                paragraphs.append(f"[{_clock(mark)}] " + " ".join(current))
            current, mark = [], s["start"]
        current.append(s["text"])
    if current:
        paragraphs.append(f"[{_clock(mark)}] " + " ".join(current))
    return "\n\n".join(paragraphs) + "\n"


def to_srt(segments):
    blocks = []
    for i, s in enumerate(segments, 1):
        blocks.append(f"{i}\n{_ts(s['start'], ',')} --> {_ts(s['end'], ',')}\n{s['text']}\n")
    return "\n".join(blocks)


def to_vtt(segments):
    blocks = ["WEBVTT\n"]
    for s in segments:
        blocks.append(f"{_ts(s['start'], '.')} --> {_ts(s['end'], '.')}\n{s['text']}\n")
    return "\n".join(blocks)


def _atomic_write(path, content):
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def output_paths(output_dir, out_rel):
    base = Path(output_dir) / out_rel
    return {fmt: base.with_name(base.name + "." + fmt) for fmt in FORMATS}


def render(doc, fmt, txt_timestamps=False):
    segments = doc.get("segments", [])
    if fmt == "txt":
        return to_txt(segments, txt_timestamps)
    if fmt == "srt":
        return to_srt(segments)
    if fmt == "vtt":
        return to_vtt(segments)
    return json.dumps(doc, ensure_ascii=False, indent=2)


def write_outputs(output_dir, out_rel, result, meta, formats=FORMATS, txt_timestamps=False, internal_path=None,
                  replacements=None):
    """Writes the selected formats. TXT is always written, last, and marks a complete transcription.
    The full result is additionally kept at internal_path so every format stays downloadable."""
    paths = output_paths(output_dir, out_rel)
    paths["txt"].parent.mkdir(parents=True, exist_ok=True)
    segments = clean_segments(result, replacements)
    doc = {
        **meta,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "text": " ".join(s["text"] for s in segments),
        "segments": segments,
    }
    if internal_path:
        Path(internal_path).parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(Path(internal_path), render(doc, "json"))
    for fmt in ("json", "srt", "vtt"):
        if fmt in formats:
            _atomic_write(paths[fmt], render(doc, fmt))
    _atomic_write(paths["txt"], render(doc, "txt", txt_timestamps))
    return doc


def outputs_complete(output_dir, out_rel):
    return output_paths(output_dir, out_rel)["txt"].exists()


def read_json(output_dir, out_rel):
    p = output_paths(output_dir, out_rel)["json"]
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_merged(path, entries):
    """entries: list of (heading, text)."""
    parts = []
    for heading, text in entries:
        bar = "=" * max(20, len(heading))
        parts.append(f"{bar}\n{heading}\n{bar}\n\n{text.strip()}\n")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, "\n\n".join(parts))
    return path
