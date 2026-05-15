"""FastAPI server for the PDF selective-text-rebuild UI."""

from __future__ import annotations

import asyncio
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
_preset_store = PresetStore(_PRESETS_DIR)


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
        self._undo: list[dict[str, str]] = []

    # ── Undo ──────────────────────────────────────────────────────────────────

    def push_undo(self) -> None:
        self._undo.append(dict(self.state))
        if len(self._undo) > 100:
            self._undo.pop(0)

    def undo(self) -> bool:
        if not self._undo:
            return False
        self.state = self._undo.pop()
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
                sid = s["id"]
                spans_out.append({
                    "id":          sid,
                    "text":        s["text"],
                    "bbox":        s["bbox"],
                    "size":        s.get("size", 12),
                    "dir":         s.get("dir", [1, 0]),
                    "state":       self.state.get(sid, "delete"),
                    "edited_text": self.edited_texts.get(sid),
                })
            # Custom spans belonging to this page
            prefix = f"p{page_idx}_"
            for sid, span in self.custom_spans.items():
                if sid.startswith(prefix):
                    spans_out.append({
                        "id":          sid,
                        "text":        span["text"],
                        "bbox":        span["bbox"],
                        "size":        span.get("size", 12),
                        "dir":         span.get("dir", [1, 0]),
                        "state":       self.state.get(sid, "delete"),
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

class StateRequest(BaseModel):
    span_ids: list[str]
    action: str  # "keep" | "delete"

class EditRequest(BaseModel):
    span_id: str
    text: str

class MergeRequest(BaseModel):
    span_ids: list[str]

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
            if classify_span(span, compiled, req.granularity) == "keep":
                matched.append(span["id"])

    prefix_map: dict[str, dict] = {}
    for sid, span in session.custom_spans.items():
        if classify_span(span, compiled, req.granularity) == "keep":
            matched.append(sid)

    return JSONResponse({"matched_ids": matched, "total_matched": len(matched)})


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
        session.state[req.span_id] = "keep"   # editing implies keep
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
        session.state[sid] = "delete"
    session.custom_spans[new_id] = new_span
    session.state[new_id] = "keep"

    return JSONResponse({
        "ok":          True,
        "new_span": {
            "id":          new_id,
            "text":        merged_text,
            "bbox":        [x0, y0, x1, y1],
            "size":        avg_size,
            "dir":         new_span["dir"],
            "state":       "keep",
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
    return JSONResponse(_preset_store.all())

@app.post("/api/presets")
def save_preset(req: SavePresetRequest) -> JSONResponse:
    _preset_store.save(req.name, {
        "patterns": req.patterns, "case_insensitive": req.case_insensitive,
        "multiline": req.multiline, "dotall": req.dotall, "granularity": req.granularity,
    })
    return JSONResponse({"ok": True})

@app.delete("/api/presets/{name}")
def delete_preset(name: str) -> JSONResponse:
    if not _preset_store.delete(name):
        raise HTTPException(404, "Preset not found")
    return JSONResponse({"ok": True})


# ── Static files ───────────────────────────────────────────────────────────────

app.mount("/", StaticFiles(directory=str(_HERE / "static"), html=True), name="static")
