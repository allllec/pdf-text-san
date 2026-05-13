"""FastAPI server for the PDF selective-text-rebuild UI."""

from __future__ import annotations

import asyncio
import json
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

from .classifier import (
    PresetStore,
    apply_classification,
    build_flags,
    validate_patterns,
)
from .extractor import extract_text, save_sidecar
from .outliner import outline_text
from .pipeline import render_pages, render_thumbnails
from .reinsert import reinsert_text, verify_roundtrip

# ── App setup ──────────────────────────────────────────────────────────────────

app = FastAPI(title="PDF Text Sanitiser")

_HERE = Path(__file__).parent
_SESSIONS_DIR = Path("sessions")
_PRESETS_DIR = Path.home() / ".pdfsan" / "presets"
_SESSIONS_DIR.mkdir(exist_ok=True)

_executor = ThreadPoolExecutor(max_workers=4)

# In-memory session registry: session_id → SessionState
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
        self.session_id = session_id
        self.session_dir = session_dir
        self.input_path = input_path
        self.outlined_path = outlined_path
        self.pages_data = pages_data
        self.filename = filename
        self.page_count = len(pages_data)

        # Classification state
        # auto_cls: the regex-derived state (reset on each regex application)
        self.auto_cls: dict[str, dict[str, str]] = {
            pid: {s["id"]: "keep" for s in page["spans"]}
            for pid, page in pages_data.items()
        }
        # overrides: manual toggles (preserved across regex re-application)
        self.overrides: dict[str, str] = {}
        # edited texts: {span_id: new_text}
        self.edited_texts: dict[str, str] = {}

        # Ready flag — set to True once PNGs are rendered
        self.ready = False
        self.error: str | None = None

        # Undo stack (list of override snapshots)
        self._undo_stack: list[tuple[dict[str, str], dict[str, str]]] = []

    def effective(self, span_id: str, page_idx_str: str) -> str:
        if span_id in self.overrides:
            return self.overrides[span_id]
        return self.auto_cls.get(page_idx_str, {}).get(span_id, "keep")

    def is_overridden(self, span_id: str, page_idx_str: str) -> bool:
        if span_id not in self.overrides:
            return False
        auto = self.auto_cls.get(page_idx_str, {}).get(span_id, "keep")
        return self.overrides[span_id] != auto

    def push_undo(self) -> None:
        self._undo_stack.append((dict(self.overrides), dict(self.edited_texts)))
        if len(self._undo_stack) > 100:
            self._undo_stack.pop(0)

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        self.overrides, self.edited_texts = self._undo_stack.pop()
        return True

    def page_image_path(self, page_idx: int) -> Path:
        return self.session_dir / "pages" / f"page_{page_idx:04d}.png"

    def thumb_path(self, page_idx: int) -> Path:
        return self.session_dir / "thumbs" / f"page_{page_idx:04d}.png"

    def counters(self) -> dict:
        per_page: list[dict] = []
        total_spans = total_kept = total_deleted = total_overridden = 0

        for pid, page in self.pages_data.items():
            spans = page["spans"]
            kept = deleted = overridden = 0
            for s in spans:
                sid = s["id"]
                eff = self.effective(sid, pid)
                auto = self.auto_cls.get(pid, {}).get(sid, "keep")
                is_manual = sid in self.overrides and self.overrides[sid] != auto
                if eff == "keep":
                    kept += 1
                else:
                    deleted += 1
                if is_manual:
                    overridden += 1
            per_page.append(
                {
                    "page": int(pid),
                    "total": len(spans),
                    "kept": kept,
                    "deleted": deleted,
                    "overridden": overridden,
                }
            )
            total_spans += len(spans)
            total_kept += kept
            total_deleted += deleted
            total_overridden += overridden

        return {
            "total": total_spans,
            "kept": total_kept,
            "deleted": total_deleted,
            "overridden": total_overridden,
            "per_page": per_page,
        }

    def page_spans_payload(self, page_idx: int) -> dict:
        pid = str(page_idx)
        page = self.pages_data.get(pid, {})
        spans_out = []
        for s in page.get("spans", []):
            sid = s["id"]
            auto = self.auto_cls.get(pid, {}).get(sid, "keep")
            eff = self.effective(sid, pid)
            is_override = sid in self.overrides and self.overrides[sid] != auto
            spans_out.append(
                {
                    "id": sid,
                    "text": s["text"],
                    "edited_text": self.edited_texts.get(sid),
                    "bbox": s["bbox"],
                    "dir": s.get("dir", [1, 0]),
                    "size": s.get("size", 12),
                    "classification": auto,
                    "effective": eff,
                    "overridden": is_override,
                }
            )
        return {
            "page_idx": page_idx,
            "spans": spans_out,
            "page_width": page.get("page_width", 595),
            "page_height": page.get("page_height", 842),
        }


# ── Background tasks ───────────────────────────────────────────────────────────

def _prepare_session(session: SessionState) -> None:
    """Run in thread pool: outline + render pages + thumbnails."""
    try:
        outline_text(session.input_path, session.outlined_path)
        render_pages(
            session.outlined_path,
            session.session_dir / "pages",
            scale=2.0,
        )
        render_thumbnails(
            session.outlined_path,
            session.session_dir / "thumbs",
            scale=0.15,
        )
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
    granularity: str = "span"  # "span" | "word"


class ToggleRequest(BaseModel):
    span_ids: list[str]
    force: str | None = None  # "keep" | "delete" | None = toggle


class EditRequest(BaseModel):
    span_id: str
    text: str  # "" = revert to original


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
        raise HTTPException(503, "Session still preparing — try again in a moment")


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
) -> JSONResponse:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")

    session_id = str(uuid.uuid4())
    session_dir = _SESSIONS_DIR / session_id
    session_dir.mkdir(parents=True)

    input_path = session_dir / "original.pdf"
    outlined_path = session_dir / "outlined.pdf"

    content = await file.read()
    input_path.write_bytes(content)

    # Extract text synchronously (fast) then prepare asynchronously
    loop = asyncio.get_event_loop()
    pages_data = await loop.run_in_executor(_executor, extract_text, input_path)
    save_sidecar(pages_data, session_dir / "spans.json")

    session = SessionState(
        session_id=session_id,
        session_dir=session_dir,
        input_path=input_path,
        outlined_path=outlined_path,
        pages_data=pages_data,
        filename=file.filename,
    )
    _sessions[session_id] = session

    background_tasks.add_task(_prepare_session, session)

    return JSONResponse(
        {
            "session_id": session_id,
            "page_count": session.page_count,
            "filename": file.filename,
        }
    )


@app.get("/api/{session_id}/status")
def session_status(session_id: str) -> JSONResponse:
    session = _get_session(session_id)
    return JSONResponse(
        {
            "ready": session.ready,
            "error": session.error,
            "page_count": session.page_count,
        }
    )


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


@app.get("/api/{session_id}/page/{page_idx}/spans")
def page_spans(session_id: str, page_idx: int) -> JSONResponse:
    session = _get_session(session_id)
    return JSONResponse(session.page_spans_payload(page_idx))


@app.get("/api/{session_id}/counters")
def counters(session_id: str) -> JSONResponse:
    session = _get_session(session_id)
    return JSONResponse(session.counters())


@app.post("/api/{session_id}/regex")
def apply_regex(session_id: str, req: RegexRequest) -> JSONResponse:
    session = _get_session(session_id)

    errors = validate_patterns(req.patterns)
    if errors:
        raise HTTPException(422, {"pattern_errors": errors})

    flags = build_flags(req.case_insensitive, req.multiline, req.dotall)
    session.auto_cls = apply_classification(
        session.pages_data, req.patterns, flags, req.granularity
    )

    # Reset auto_cls only — manual overrides are preserved per spec
    return JSONResponse({"ok": True, "counters": session.counters()})


@app.post("/api/{session_id}/toggle")
def toggle_spans(session_id: str, req: ToggleRequest) -> JSONResponse:
    session = _get_session(session_id)
    session.push_undo()

    changes: dict[str, str] = {}
    for sid in req.span_ids:
        # Find page for this span to look up auto classification
        pid = sid.split("_s")[0][1:]  # "p0_s3" → "0"
        auto = session.auto_cls.get(pid, {}).get(sid, "keep")

        if req.force is not None:
            new_state = req.force
        else:
            cur = session.overrides.get(sid, auto)
            new_state = "delete" if cur == "keep" else "keep"

        if new_state == auto:
            session.overrides.pop(sid, None)
        else:
            session.overrides[sid] = new_state

        # Recalculate effective after override logic
        changes[sid] = session.overrides.get(sid, auto)

    return JSONResponse({"ok": True, "changes": changes})


@app.post("/api/{session_id}/edit")
def edit_span(session_id: str, req: EditRequest) -> JSONResponse:
    session = _get_session(session_id)
    session.push_undo()

    if req.text:
        session.edited_texts[req.span_id] = req.text
        # Editing implies keep
        pid = req.span_id.split("_s")[0][1:]
        auto = session.auto_cls.get(pid, {}).get(req.span_id, "keep")
        if auto != "keep":
            session.overrides[req.span_id] = "keep"
    else:
        session.edited_texts.pop(req.span_id, None)

    return JSONResponse({"ok": True})


@app.post("/api/{session_id}/undo")
def undo(session_id: str) -> JSONResponse:
    session = _get_session(session_id)
    ok = session.undo()
    return JSONResponse({"ok": ok})


@app.post("/api/{session_id}/reset_overrides")
def reset_overrides(session_id: str) -> JSONResponse:
    session = _get_session(session_id)
    session.push_undo()
    session.overrides.clear()
    session.edited_texts.clear()
    return JSONResponse({"ok": True, "counters": session.counters()})


@app.post("/api/{session_id}/save")
async def save_pdf(session_id: str, req: SaveRequest) -> JSONResponse:
    session = _get_session(session_id)
    _assert_ready(session)

    stem = Path(session.filename).stem
    out_name = req.filename or f"{stem}_rebuilt.pdf"
    out_path = session.session_dir / "output" / out_name
    out_path.parent.mkdir(exist_ok=True)

    loop = asyncio.get_event_loop()
    inserted = await loop.run_in_executor(
        _executor,
        lambda: reinsert_text(
            session.outlined_path,
            session.pages_data,
            session.auto_cls,
            session.overrides,
            session.edited_texts,
            out_path,
        ),
    )

    issues = verify_roundtrip(out_path, inserted, session.pages_data)
    kept = sum(len(v) for v in inserted.values())

    return JSONResponse(
        {
            "ok": True,
            "download_path": str(out_path),
            "download_url": f"/api/{session_id}/download",
            "kept": kept,
            "issues": issues,
        }
    )


@app.get("/api/{session_id}/download")
def download(session_id: str) -> FileResponse:
    session = _get_session(session_id)
    out_dir = session.session_dir / "output"
    files = list(out_dir.glob("*.pdf")) if out_dir.exists() else []
    if not files:
        raise HTTPException(404, "No output PDF yet — call /save first")
    latest = max(files, key=lambda p: p.stat().st_mtime)
    return FileResponse(
        latest,
        media_type="application/pdf",
        filename=latest.name,
        headers={"Content-Disposition": f'attachment; filename="{latest.name}"'},
    )


# ── Preset routes ──────────────────────────────────────────────────────────────

@app.get("/api/presets")
def list_presets() -> JSONResponse:
    return JSONResponse(_preset_store.all())


@app.post("/api/presets")
def save_preset(req: SavePresetRequest) -> JSONResponse:
    _preset_store.save(
        req.name,
        {
            "patterns": req.patterns,
            "case_insensitive": req.case_insensitive,
            "multiline": req.multiline,
            "dotall": req.dotall,
            "granularity": req.granularity,
        },
    )
    return JSONResponse({"ok": True})


@app.delete("/api/presets/{name}")
def delete_preset(name: str) -> JSONResponse:
    ok = _preset_store.delete(name)
    if not ok:
        raise HTTPException(404, "Preset not found")
    return JSONResponse({"ok": True})


# ── Static files (served last to avoid shadowing API routes) ───────────────────

app.mount("/", StaticFiles(directory=str(_HERE / "static"), html=True), name="static")
