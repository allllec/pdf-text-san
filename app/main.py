"""FastAPI server for the PDF selective-text-rebuild UI."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pymupdf
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .classifier import PresetStore, compile_patterns, classify_span, build_flags, validate_patterns
from .extractor import extract_text, save_sidecar
from .outliner import outline_text
from .pipeline import render_pages, render_thumbnails
from .reinsert import reinsert_text, verify_roundtrip

# ── App setup ──────────────────────────────────────────────────────────────────

app = FastAPI(title="PDF Text Sanitiser")

_HERE        = Path(__file__).parent
_SESSIONS_DIR = Path("sessions")
_PRESETS_DIR  = Path.home() / ".pdfsan" / "presets"
_SESSIONS_DIR.mkdir(exist_ok=True)

_executor     = ThreadPoolExecutor(max_workers=4)
_sessions: dict[str, "SessionState"] = {}
_regex_presets = PresetStore(_PRESETS_DIR, "regex_presets.json")
_fr_presets    = PresetStore(_PRESETS_DIR, "fr_presets.json")


def _discover_sessions():
    """Scan sessions directory and rebuild SessionState objects for existing work."""
    if not _SESSIONS_DIR.exists():
        return
    for sdir in _SESSIONS_DIR.iterdir():
        if not sdir.is_dir(): continue
        # Basic validation that it's a valid session folder
        spans_path = sdir / "spans.json"
        orig_path  = sdir / "original.pdf"
        if not spans_path.exists() or not orig_path.exists():
            continue

        try:
            with open(spans_path, "r") as f:
                pages_data = json.load(f)
            
            session_id = sdir.name
            outlined_path = sdir / "outlined.pdf"
            # We don't know the original filename easily unless we saved it, 
            # for now we'll use the folder name or 'recovered_document.pdf'
            filename = "recovered_document.pdf"
            
            session = SessionState(session_id, sdir, orig_path, outlined_path, pages_data, filename)
            # If the outlined file and pages exist, mark as ready
            if outlined_path.exists() and (sdir / "pages").exists():
                session.ready = True
            
            _sessions[session_id] = session
        except Exception as e:
            print(f"Failed to recover session {sdir.name}: {e}")


# ── Session state ──────────────────────────────────────────────────────────────

class SessionState:
    def __init__(
        self,
        session_id: str,
        session_dir: Path,
        input_path: Path,
        outlined_path: Path,
        pages_data: dict,
        filename: str,
    ) -> None:
        self.session_id   = session_id
        self.session_dir  = session_dir
        self.input_path   = input_path
        self.outlined_path = outlined_path
        self.pages_data   = pages_data
        self.filename     = filename
        self.page_count   = len(pages_data)
        self.ready        = False
        self.error: str | None = None

        # Flat unified state — default "delete" for every span
        self.state: dict[str, str] = {
            s["id"]: "delete"
            for page in pages_data.values()
            for s in page["spans"]
        }
        # Merged / custom spans added at runtime
        self.custom_spans: dict[str, dict] = {}
        self._merge_counter = 0

        self.edited_texts: dict[str, str] = {}
        self._undo: list[dict[str, Any]] = []

    # ── Undo ──────────────────────────────────────────────────────────────────

    def push_undo(self) -> None:
        self._undo.append({
            "state": dict(self.state),
            "edited_texts": dict(self.edited_texts),
            "custom_spans": copy.deepcopy(self.custom_spans),
            "merge_counter": self._merge_counter,
        })
        if len(self._undo) > 100:
            self._undo.pop(0)

    def undo(self) -> bool:
        if not self._undo:
            return False
        snapshot = self._undo.pop()
        self.state = snapshot["state"]
        self.edited_texts = snapshot["edited_texts"]
        self.custom_spans = snapshot["custom_spans"]
        self._merge_counter = snapshot["merge_counter"]
        return True

    # ── Paths ─────────────────────────────────────────────────────────────────

    def page_image_path(self, page_idx: int) -> Path:
        return self.session_dir / "pages" / f"page_{page_idx:04d}.png"

    def thumb_path(self, page_idx: int) -> Path:
        return self.session_dir / "thumbs" / f"page_{page_idx:04d}.png"

    # ── Payloads ──────────────────────────────────────────────────────────────

    def all_spans_payload(self) -> dict:
        """All pages + spans in one response for initial load."""
        result: dict[str, Any] = {}
        for pid, page in self.pages_data.items():
            page_idx = int(pid)
            spans_out = []
            for s in page["spans"]:
                if not s["text"].strip(): continue
                sid = s["id"]
                state = self.state.get(sid, "delete")
                if state == "hidden": continue # Skip spans that were split/merged
                
                spans_out.append({
                    "id":          sid,
                    "text":        s["text"],
                    "bbox":        s["bbox"],
                    "size":        s.get("size", 12),
                    "dir":         s.get("dir", [1, 0]),
                    "state":       state,
                    "edited_text": self.edited_texts.get(sid),
                })
            # Custom spans
            prefix = f"p{page_idx}_"
            for sid, span in self.custom_spans.items():
                if sid.startswith(prefix):
                    state = self.state.get(sid, "delete")
                    if state == "hidden": continue
                    spans_out.append({
                        "id":          sid,
                        "text":        span["text"],
                        "bbox":        span["bbox"],
                        "size":        span.get("size", 12),
                        "dir":         span.get("dir", [1, 0]),
                        "state":       state,
                        "edited_text": self.edited_texts.get(sid),
                        "merged":      True,
                    })
            result[pid] = {
                "spans":       spans_out,
                "page_width":  page.get("page_width", 595),
                "page_height": page.get("page_height", 842),
            }
        return result

    def counters(self) -> dict:
        kept = sum(1 for v in self.state.values() if v == "keep")
        total = len(self.state)
        return {"total": total, "kept": kept, "deleted": total - kept}


_discover_sessions()


# ── Background preparation ─────────────────────────────────────────────────────

def _prepare_session(session: SessionState) -> None:
    try:
        outline_text(session.input_path, session.outlined_path)
        render_pages(session.outlined_path, session.session_dir / "pages", scale=2.0)
        render_thumbnails(session.outlined_path, session.session_dir / "thumbs", scale=0.15)
        session.ready = True
    except Exception as exc:
        session.error = str(exc)
        print(f"[session {session.session_id}] preparation failed: {exc}")


# ── Request / response models ──────────────────────────────────────────────────

class RegexRequest(BaseModel):
    patterns: list[str] = []
    case_insensitive: bool = False
    multiline: bool = False
    dotall: bool = False
    granularity: str = "span"
    split_matches: bool = False

class RegexSplitRequest(BaseModel):
    pattern: str
    case_insensitive: bool = False
    multiline: bool = False
    dotall: bool = False

class FindReplaceRequest(BaseModel):
    find: str
    replace: str
    case_insensitive: bool = False

class StateRequest(BaseModel):
    span_ids: list[str]
    action: str  # "keep" | "delete"

class EditRequest(BaseModel):
    span_id: str
    text: str

class MergeRequest(BaseModel):
    span_ids: list[str]

class SplitRequest(BaseModel):
    span_ids: list[str]
    pattern: str
    case_insensitive: bool = False
    multiline: bool = False
    dotall: bool = False

class SavePresetRequest(BaseModel):
    name: str
    patterns: list[str]
    case_insensitive: bool = False
    multiline: bool = False
    dotall: bool = False
    granularity: str = "span"

class SaveRequest(BaseModel):
    filename: str | None = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_session(session_id: str) -> SessionState:
    if session_id not in _sessions:
        raise HTTPException(404, "Session not found")
    return _sessions[session_id]

def _assert_ready(session: SessionState) -> None:
    if session.error:
        raise HTTPException(500, f"Session preparation failed: {session.error}")
    if not session.ready:
        raise HTTPException(503, "Session still preparing — try again shortly")

def _find_span(session: SessionState, sid: str) -> dict | None:
    pid = sid.split("_")[0][1:]
    page = session.pages_data.get(pid, {})
    span = next((s for s in page.get("spans", []) if s["id"] == sid), None)
    return span or session.custom_spans.get(sid)


def _active_span_ids(session: SessionState) -> list[str]:
    return [sid for sid, st in session.state.items() if st != "hidden"]


def _replacement_to_python_template(replace: str) -> str:
    """Translate JS-style replacement tokens to Python-compatible template.

    Supported tokens:
      $$  -> literal $
      $&  -> whole match
      $1  -> capture group 1
      ${name} -> named capture group
    """
    marker = "\x00"
    out = replace.replace("$$", marker)
    out = out.replace("$&", r"\g<0>")
    out = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", r"\\g<\1>", out)
    out = re.sub(r"\$(\d+)", r"\\g<\1>", out)
    return out.replace(marker, "$")


def _split_span_by_pattern(span: dict, pattern: re.Pattern) -> list[dict[str, Any]]:
    """Return ordered parts with text and whether each part matched the regex."""
    text = span["text"]
    parts: list[dict[str, Any]] = []
    last_end = 0
    for m in pattern.finditer(text):
        start, end = m.span()
        if start > last_end:
            parts.append({"text": text[last_end:start], "is_match": False, "start": last_end, "end": start})
        parts.append({"text": text[start:end], "is_match": True, "start": start, "end": end})
        last_end = end
    if last_end < len(text):
        parts.append({"text": text[last_end:], "is_match": False, "start": last_end, "end": len(text)})
    return parts


def _split_spans_with_regex(
    session: SessionState,
    span_ids: list[str],
    pattern: re.Pattern,
) -> tuple[list[dict], list[str]]:
    """Split matching spans into before/match/after pieces, preserving status."""
    new_spans: list[dict] = []
    removed_ids: list[str] = []

    for sid in span_ids:
        if session.state.get(sid, "delete") == "hidden":
            continue
        span = _find_span(session, sid)
        if not span:
            continue

        src_text = session.edited_texts.get(sid, span["text"])
        parts = _split_span_by_pattern({**span, "text": src_text}, pattern)
        if len(parts) <= 1 or not any(p["is_match"] for p in parts):
            continue

        parent_state = session.state.get(sid, "delete")
        bbox = span.get("bbox", [0, 0, 0, 0])
        x0, y0, x1, y1 = bbox
        total_chars = max(len(src_text), 1)
        total_width = max(float(x1) - float(x0), 0.0)
        origin_y = span.get("origin", [x0, y1])[1]

        removed_ids.append(sid)
        session.state[sid] = "hidden"
        session.edited_texts.pop(sid, None)

        for i, part in enumerate(parts):
            part_text = part["text"]
            if not part_text.strip():
                continue

            start = part["start"]
            end = part["end"]
            part_x0 = x0 + total_width * (start / total_chars)
            part_x1 = x0 + total_width * (end / total_chars)
            if part_x1 <= part_x0:
                continue

            new_id = f"{sid}_s{i}"
            new_span = {
                "id": new_id,
                "text": part_text,
                "bbox": [part_x0, y0, part_x1, y1],
                "origin": [part_x0, origin_y],
                "font": span.get("font", ""),
                "size": span.get("size", 12),
                "color": span.get("color", 0),
                "dir": span.get("dir", [1, 0]),
                "flags": span.get("flags", 0),
                "ascender": span.get("ascender", 0.8),
                "descender": span.get("descender", -0.2),
            }
            session.custom_spans[new_id] = new_span
            session.state[new_id] = parent_state

            new_spans.append({
                "id": new_id,
                "text": part_text,
                "bbox": new_span["bbox"],
                "size": new_span["size"],
                "state": parent_state,
                "page": int(sid.split("_")[0][1:]),
            })

    return new_spans, removed_ids


# ── Upload ─────────────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload_pdf(background_tasks: BackgroundTasks, file: UploadFile = File(...)) -> JSONResponse:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")

    session_id = str(uuid.uuid4())
    session_dir = _SESSIONS_DIR / session_id
    session_dir.mkdir(parents=True)

    input_path    = session_dir / "original.pdf"
    outlined_path = session_dir / "outlined.pdf"
    input_path.write_bytes(await file.read())

    loop = asyncio.get_event_loop()
    pages_data = await loop.run_in_executor(_executor, extract_text, input_path)
    save_sidecar(pages_data, session_dir / "spans.json")

    session = SessionState(session_id, session_dir, input_path, outlined_path, pages_data, file.filename)
    _sessions[session_id] = session
    background_tasks.add_task(_prepare_session, session)

    return JSONResponse({"session_id": session_id, "page_count": session.page_count, "filename": file.filename})


# ── Session management ─────────────────────────────────────────────────────────

@app.get("/api/sessions")
def list_sessions() -> JSONResponse:
    result = []
    for sid, session in _sessions.items():
        result.append({
            "session_id": sid,
            "filename":   session.filename,
            "page_count": session.page_count,
            "ready":      session.ready,
            "error":      session.error,
            "counters":   session.counters(),
        })
    return JSONResponse(result)

@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str) -> JSONResponse:
    session = _get_session(session_id)
    del _sessions[session_id]
    shutil.rmtree(session.session_dir, ignore_errors=True)
    return JSONResponse({"ok": True})

@app.get("/api/{session_id}/status")
def session_status(session_id: str) -> JSONResponse:
    session = _get_session(session_id)
    return JSONResponse({"ready": session.ready, "error": session.error, "page_count": session.page_count})


# ── Page assets ───────────────────────────────────────────────────────────────

@app.get("/api/{session_id}/page/{page_idx}/image")
def page_image(session_id: str, page_idx: int) -> FileResponse:
    session = _get_session(session_id)
    _assert_ready(session)
    path = session.page_image_path(page_idx)
    if not path.exists():
        raise HTTPException(404, f"Page {page_idx} image not found")
    return FileResponse(path, media_type="image/png")

@app.get("/api/{session_id}/page/{page_idx}/thumb")
def page_thumb(session_id: str, page_idx: int) -> FileResponse:
    session = _get_session(session_id)
    _assert_ready(session)
    path = session.thumb_path(page_idx)
    if not path.exists():
        raise HTTPException(404, f"Page {page_idx} thumbnail not found")
    return FileResponse(path, media_type="image/png")


# ── Spans ─────────────────────────────────────────────────────────────────────

@app.get("/api/{session_id}/spans")
def all_spans(session_id: str) -> JSONResponse:
    """Return all pages' spans + dimensions in one call for the scroll view."""
    session = _get_session(session_id)
    return JSONResponse(session.all_spans_payload())

@app.get("/api/{session_id}/counters")
def counters(session_id: str) -> JSONResponse:
    return JSONResponse(_get_session(session_id).counters())


# ── Regex (returns selection IDs, does not change state) ──────────────────────

@app.post("/api/{session_id}/regex")
def apply_regex(session_id: str, req: RegexRequest) -> JSONResponse:
    session = _get_session(session_id)
    errors = validate_patterns(req.patterns)
    if errors:
        raise HTTPException(422, {"pattern_errors": errors})

    flags    = build_flags(req.case_insensitive, req.multiline, req.dotall)
    compiled = compile_patterns(req.patterns, flags)
    matched: list[str] = []

    for pid, page in session.pages_data.items():
        for span in page["spans"]:
            if session.state.get(span["id"], "delete") == "hidden":
                continue
            if classify_span(span, compiled, req.granularity) == "keep":
                matched.append(span["id"])

    for sid, span in session.custom_spans.items():
        if session.state.get(sid, "delete") == "hidden":
            continue
        if classify_span(span, compiled, req.granularity) == "keep":
            matched.append(sid)

    return JSONResponse({"matched_ids": matched, "total_matched": len(matched)})


@app.post("/api/{session_id}/find-replace")
def find_replace(session_id: str, req: FindReplaceRequest) -> JSONResponse:
    session = _get_session(session_id)
    flags = re.IGNORECASE if req.case_insensitive else 0
    try:
        pattern = re.compile(req.find, flags)
    except re.error as exc:
        raise HTTPException(422, f"Invalid find regex: {exc}")

    changes: dict[str, str] = {}
    session.push_undo()
    replace_template = _replacement_to_python_template(req.replace)

    for sid in _active_span_ids(session):
        span = _find_span(session, sid)
        if not span:
            continue

        src_text = session.edited_texts.get(sid, span["text"])
        if not pattern.search(src_text):
            continue

        try:
            new_text = pattern.sub(lambda m: m.expand(replace_template), src_text)
        except re.error as exc:
            raise HTTPException(422, f"Invalid replacement: {exc}")

        if new_text == src_text:
            continue

        if new_text == span["text"]:
            session.edited_texts.pop(sid, None)
        else:
            session.edited_texts[sid] = new_text
        changes[sid] = new_text

    return JSONResponse({"ok": True, "changes": changes, "counters": session.counters()})


@app.post("/api/{session_id}/regex-split")
def regex_split(session_id: str, req: RegexSplitRequest) -> JSONResponse:
    session = _get_session(session_id)
    flags = build_flags(req.case_insensitive, req.multiline, req.dotall)
    try:
        pattern = re.compile(req.pattern, flags)
    except re.error as exc:
        raise HTTPException(422, f"Invalid regex: {exc}")

    session.push_undo()
    matched_ids: list[str] = []
    for sid in _active_span_ids(session):
        span = _find_span(session, sid)
        if not span:
            continue
        if pattern.search(session.edited_texts.get(sid, span["text"])):
            matched_ids.append(sid)

    new_spans, removed_ids = _split_spans_with_regex(session, matched_ids, pattern)
    return JSONResponse({
        "ok": True, 
        "new_spans": new_spans, 
        "removed_ids": removed_ids,
        "counters": session.counters()
    })


@app.post("/api/{session_id}/split")
def split_selected(session_id: str, req: SplitRequest) -> JSONResponse:
    """Split selected spans by regex into before/match/after parts."""
    session = _get_session(session_id)
    if not req.pattern.strip():
        raise HTTPException(400, "Split requires a regex pattern")

    flags = build_flags(req.case_insensitive, req.multiline, req.dotall)
    try:
        pattern = re.compile(req.pattern, flags)
    except re.error as exc:
        raise HTTPException(422, f"Invalid regex: {exc}")

    session.push_undo()
    new_spans, removed_ids = _split_spans_with_regex(session, req.span_ids, pattern)
    return JSONResponse({"ok": True, "new_spans": new_spans, "removed_ids": removed_ids, "counters": session.counters()})


# ── State mutations ────────────────────────────────────────────────────────────

@app.post("/api/{session_id}/state")
def set_state(session_id: str, req: StateRequest) -> JSONResponse:
    """Set span_ids to 'keep' or 'delete'."""
    if req.action not in ("keep", "delete"):
        raise HTTPException(400, "action must be 'keep' or 'delete'")
    session = _get_session(session_id)
    session.push_undo()
    for sid in req.span_ids:
        session.state[sid] = req.action
    return JSONResponse({"ok": True, "counters": session.counters()})


@app.post("/api/{session_id}/edit")
def edit_span(session_id: str, req: EditRequest) -> JSONResponse:
    session = _get_session(session_id)
    session.push_undo()
    if req.text:
        session.edited_texts[req.span_id] = req.text
    else:
        session.edited_texts.pop(req.span_id, None)
    return JSONResponse({"ok": True})


@app.post("/api/{session_id}/merge")
def merge_spans(session_id: str, req: MergeRequest) -> JSONResponse:
    session = _get_session(session_id)
    if len(req.span_ids) < 2:
        raise HTTPException(400, "Need at least 2 spans to merge")

    spans: list[dict] = []
    for sid in req.span_ids:
        span = _find_span(session, sid)
        if span:
            spans.append(span)

    if not spans:
        raise HTTPException(400, "No valid spans found")

    statuses = {session.state.get(sid, "delete") for sid in req.span_ids if session.state.get(sid) != "hidden"}
    statuses.discard("hidden")
    if len(statuses) > 1:
        raise HTTPException(400, "Selected spans have mixed status. Apply A or D first, then merge.")
    inherited_state = next(iter(statuses), "delete")

    # Warn if spans appear far apart (more than 3× the average height apart)
    avg_h = sum(s["bbox"][3] - s["bbox"][1] for s in spans) / len(spans)
    ys    = [s["bbox"][1] for s in spans]
    warning = "Spans appear far apart — check result" if (max(ys) - min(ys)) > avg_h * 3 else None

    # Sort left-to-right, top-to-bottom
    spans.sort(key=lambda s: (round(s["bbox"][1] / max(avg_h, 1)), s["bbox"][0]))

    merged_text = "".join(s["text"] for s in spans)
    x0 = min(s["bbox"][0] for s in spans)
    y0 = min(s["bbox"][1] for s in spans)
    x1 = max(s["bbox"][2] for s in spans)
    y1 = max(s["bbox"][3] for s in spans)
    avg_size = sum(s.get("size", 12) for s in spans) / len(spans)
    ref = spans[0]

    # Determine page from first span id
    pid = req.span_ids[0].split("_")[0][1:]
    new_id = f"p{pid}_m{session._merge_counter}"
    session._merge_counter += 1

    new_span: dict = {
        "id":        new_id,
        "text":      merged_text,
        "bbox":      [x0, y0, x1, y1],
        "origin":    [x0, y1],
        "font":      ref.get("font", ""),
        "size":      avg_size,
        "color":     ref.get("color", 0),
        "dir":       ref.get("dir", [1, 0]),
        "flags":     0,
        "ascender":  ref.get("ascender", 0.8),
        "descender": ref.get("descender", -0.2),
        "merged":    True,
    }

    session.push_undo()
    for sid in req.span_ids:
        session.state[sid] = "hidden"  # Hide superseded spans
    session.custom_spans[new_id] = new_span
    session.state[new_id] = inherited_state

    return JSONResponse({
        "ok":          True,
        "new_span": {
            "id":          new_id,
            "text":        merged_text,
            "bbox":        [x0, y0, x1, y1],
            "size":        avg_size,
            "dir":         new_span["dir"],
            "state":       inherited_state,
            "merged":      True,
            "page":        int(pid),
            "edited_text": None,
        },
        "removed_ids": req.span_ids,
        "warning":     warning,
        "counters":    session.counters(),
    })


@app.post("/api/{session_id}/undo")
def undo(session_id: str) -> JSONResponse:
    session = _get_session(session_id)
    ok = session.undo()
    return JSONResponse({"ok": ok, "counters": session.counters()})

@app.post("/api/{session_id}/reset")
def reset_state(session_id: str) -> JSONResponse:
    """Reset all spans back to delete."""
    session = _get_session(session_id)
    session.push_undo()
    for sid in session.state:
        session.state[sid] = "delete"
    session.edited_texts.clear()
    return JSONResponse({"ok": True, "counters": session.counters()})


# ── Save & download ────────────────────────────────────────────────────────────

@app.post("/api/{session_id}/save")
async def save_pdf(session_id: str, req: SaveRequest) -> JSONResponse:
    session = _get_session(session_id)
    _assert_ready(session)

    out_name = req.filename or (Path(session.filename).stem + "_rebuilt.pdf")
    out_path = session.session_dir / "output" / out_name
    out_path.parent.mkdir(exist_ok=True)

    loop = asyncio.get_event_loop()
    inserted = await loop.run_in_executor(
        _executor,
        lambda: reinsert_text(
            session.outlined_path, session.pages_data,
            session.state, session.edited_texts,
            session.custom_spans, out_path,
        ),
    )
    issues = verify_roundtrip(out_path, inserted, session.pages_data, session.custom_spans)
    kept   = sum(len(v) for v in inserted.values())

    return JSONResponse({
        "ok":           True,
        "download_url": f"/api/{session_id}/download",
        "kept":         kept,
        "issues":       issues,
    })

@app.get("/api/{session_id}/download")
def download(session_id: str) -> FileResponse:
    session = _get_session(session_id)
    out_dir = session.session_dir / "output"
    files   = list(out_dir.glob("*.pdf")) if out_dir.exists() else []
    if not files:
        raise HTTPException(404, "No output PDF yet — call /save first")
    latest = max(files, key=lambda p: p.stat().st_mtime)
    return FileResponse(latest, media_type="application/pdf", filename=latest.name,
                        headers={"Content-Disposition": f'attachment; filename="{latest.name}"'})


# ── Presets ────────────────────────────────────────────────────────────────────

@app.get("/api/presets")
def list_presets() -> JSONResponse:
    return JSONResponse(_regex_presets.all())

@app.post("/api/presets")
def save_preset(req: SavePresetRequest) -> JSONResponse:
    _regex_presets.save(req.name, {
        "patterns": req.patterns, "case_insensitive": req.case_insensitive,
        "multiline": req.multiline, "dotall": req.dotall, "granularity": req.granularity,
    })
    return JSONResponse({"ok": True})

@app.delete("/api/presets/{name}")
def delete_preset(name: str) -> JSONResponse:
    if not _regex_presets.delete(name):
        raise HTTPException(404, "Preset not found")
    return JSONResponse({"ok": True})

# ── Find & Replace Presets ─────────────────────────────────────────────────────

class SaveFRPresetRequest(BaseModel):
    find: str
    replace: str

@app.get("/api/presets-fr")
def list_fr_presets() -> JSONResponse:
    return JSONResponse(_fr_presets.all())

@app.post("/api/presets-fr")
def save_fr_preset(req: SaveFRPresetRequest) -> JSONResponse:
    # Use find as the key for now, or we could ask for a name
    _fr_presets.save(req.find, {"find": req.find, "replace": req.replace})
    return JSONResponse({"ok": True})

@app.delete("/api/presets-fr/{find}")
def delete_fr_preset(find: str) -> JSONResponse:
    if not _fr_presets.delete(find):
        raise HTTPException(404, "Preset not found")
    return JSONResponse({"ok": True})


# ── Static files ───────────────────────────────────────────────────────────────

app.mount("/", StaticFiles(directory=str(_HERE / "static"), html=True), name="static")
