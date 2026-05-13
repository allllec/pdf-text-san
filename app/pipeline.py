"""Headless pipeline — importable for batch / scripting use.

Quick start::

    from app.pipeline import run_headless
    run_headless("in.pdf", "out.pdf", patterns=[r"\d{4}-\d{4}", r"INV-\w+"])
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf

from .classifier import apply_classification, build_flags, PresetStore
from .extractor import extract_text, save_sidecar
from .outliner import outline_text
from .reinsert import reinsert_text, verify_roundtrip


def render_pages(pdf_path: str | Path, out_dir: str | Path, scale: float = 2.0) -> list[Path]:
    """Rasterise every page of *pdf_path* as PNG into *out_dir*.

    Returns list of PNG paths in page order.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(str(pdf_path))
    matrix = pymupdf.Matrix(scale, scale)
    paths: list[Path] = []

    for i in range(len(doc)):
        page = doc[i]
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        dest = out_dir / f"page_{i:04d}.png"
        pix.save(str(dest))
        paths.append(dest)

    doc.close()
    return paths


def render_thumbnails(pdf_path: str | Path, out_dir: str | Path, scale: float = 0.25) -> list[Path]:
    """Render small thumbnails for the sidebar strip."""
    return render_pages(pdf_path, out_dir, scale=scale)


def run_headless(
    input_pdf: str | Path,
    output_pdf: str | Path,
    patterns: list[str] | None = None,
    flags: int = 0,
    granularity: str = "span",
    preset_name: str | None = None,
    preset_dir: str | Path | None = None,
    selection_json: str | Path | None = None,
    work_dir: str | Path | None = None,
    verify: bool = True,
) -> dict:
    """Full pipeline without UI.

    Priority for what gets kept:
    1. *selection_json* (overrides dict) if supplied
    2. Regex patterns (from *patterns* or *preset_name*)
    3. Keep everything if neither provided

    Returns a summary dict with keys: ``kept``, ``deleted``, ``issues``.
    """
    input_pdf = Path(input_pdf)
    output_pdf = Path(output_pdf)

    if work_dir is None:
        import tempfile, atexit, shutil
        _tmp = tempfile.mkdtemp(prefix="pdfsan_")
        atexit.register(shutil.rmtree, _tmp, True)
        work_dir = Path(_tmp)
    else:
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Extract ─────────────────────────────────────────────────────────────
    print(f"[pipeline] Extracting text from {input_pdf.name} …")
    pages_data = extract_text(input_pdf)
    save_sidecar(pages_data, work_dir / "spans.json")

    # ── 2. Outline ─────────────────────────────────────────────────────────────
    outlined = work_dir / "outlined.pdf"
    print("[pipeline] Flattening text to outlines via Ghostscript …")
    outline_text(input_pdf, outlined)

    # ── 3. Classify ────────────────────────────────────────────────────────────
    # Resolve patterns from preset if needed
    if preset_name and not patterns:
        store = PresetStore(preset_dir or Path.home() / ".pdfsan" / "presets")
        preset = store.get(preset_name)
        if preset:
            patterns = preset.get("patterns", [])
            flags = build_flags(
                preset.get("case_insensitive", False),
                preset.get("multiline", False),
                preset.get("dotall", False),
            )
            granularity = preset.get("granularity", "span")

    if patterns:
        print(f"[pipeline] Classifying with {len(patterns)} pattern(s) …")
        auto_cls = apply_classification(pages_data, patterns, flags, granularity)
    else:
        # Keep everything
        auto_cls = {
            pid: {s["id"]: "keep" for s in page["spans"]}
            for pid, page in pages_data.items()
        }

    # Load manual overrides from selection JSON if supplied
    overrides: dict[str, str] = {}
    if selection_json:
        overrides = json.loads(Path(selection_json).read_text(encoding="utf-8"))

    # ── 4. Reinsert ────────────────────────────────────────────────────────────
    print("[pipeline] Reinserting kept spans as invisible text …")
    inserted = reinsert_text(outlined, pages_data, auto_cls, overrides, {}, output_pdf)

    # ── 5. Count + verify ──────────────────────────────────────────────────────
    kept = sum(len(v) for v in inserted.values())
    total = sum(len(p["spans"]) for p in pages_data.values())
    deleted = total - kept

    issues: list[str] = []
    if verify:
        print("[pipeline] Verifying round-trip …")
        issues = verify_roundtrip(output_pdf, inserted, pages_data)
        for issue in issues:
            print(f"  [warn] {issue}")

    print(f"[pipeline] Done → {output_pdf}  (kept {kept}/{total}, issues: {len(issues)})")
    return {"kept": kept, "deleted": deleted, "total": total, "issues": issues}
