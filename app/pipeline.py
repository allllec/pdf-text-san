"""Headless pipeline — importable for batch / scripting use.

Quick start::

    from app.pipeline import run_headless
    run_headless("in.pdf", "out.pdf", patterns=[r"\d{4}-\d{4}", r"INV-\w+"])
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf

from .classifier import apply_classification, build_flags, PresetStore, classify_span, compile_patterns
from .extractor import extract_text, save_sidecar
from .outliner import outline_text
from .reinsert import reinsert_text, verify_roundtrip


def render_pages(pdf_path: str | Path, out_dir: str | Path, scale: float = 2.0) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(str(pdf_path))
    matrix = pymupdf.Matrix(scale, scale)
    paths: list[Path] = []
    for i in range(len(doc)):
        pix = doc[i].get_pixmap(matrix=matrix, alpha=False)
        dest = out_dir / f"page_{i:04d}.png"
        pix.save(str(dest))
        paths.append(dest)
    doc.close()
    return paths


def render_thumbnails(pdf_path: str | Path, out_dir: str | Path, scale: float = 0.25) -> list[Path]:
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

    Spans matching *patterns* are kept; non-matching are dropped.
    If no patterns given, everything is kept.
    *selection_json* is a flat ``{span_id: "keep"|"delete"}`` dict that
    overrides pattern results.

    Returns a summary dict: ``kept``, ``deleted``, ``total``, ``issues``.
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

    print(f"[pipeline] Extracting text from {input_pdf.name} …")
    pages_data = extract_text(input_pdf)
    save_sidecar(pages_data, work_dir / "spans.json")

    outlined = work_dir / "outlined.pdf"
    print("[pipeline] Flattening text to outlines via Ghostscript …")
    outline_text(input_pdf, outlined)

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

    # Build flat state dict — default "delete"
    compiled = compile_patterns(patterns or [], flags)
    state: dict[str, str] = {}
    for pid, page in pages_data.items():
        for span in page["spans"]:
            sid = span["id"]
            if patterns:
                state[sid] = classify_span(span, compiled, granularity)
            else:
                state[sid] = "keep"

    if selection_json:
        overrides = json.loads(Path(selection_json).read_text(encoding="utf-8"))
        state.update(overrides)

    print("[pipeline] Reinserting kept spans as invisible text …")
    inserted = reinsert_text(outlined, pages_data, state, {}, {}, output_pdf)

    kept    = sum(len(v) for v in inserted.values())
    total   = sum(len(p["spans"]) for p in pages_data.values())
    deleted = total - kept

    issues: list[str] = []
    if verify:
        print("[pipeline] Verifying round-trip …")
        issues = verify_roundtrip(output_pdf, inserted, pages_data)
        for issue in issues:
            print(f"  [warn] {issue}")

    print(f"[pipeline] Done → {output_pdf}  (kept {kept}/{total}, issues: {len(issues)})")
    return {"kept": kept, "deleted": deleted, "total": total, "issues": issues}
