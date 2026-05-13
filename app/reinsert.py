"""Reinsert kept text as an invisible (render_mode=3) layer."""

from __future__ import annotations

from pathlib import Path

import pymupdf

_LATIN_FONT  = "helv"
_FALLBACK_FONT = "figo"


def _pick_font(text: str) -> pymupdf.Font:
    try:
        font = pymupdf.Font(_LATIN_FONT)
        if all(font.has_glyph(ord(ch)) for ch in text if not ch.isspace()):
            return font
    except Exception:
        pass
    try:
        return pymupdf.Font(_FALLBACK_FONT)
    except Exception:
        return pymupdf.Font(_LATIN_FONT)


def _append_span(tw: pymupdf.TextWriter, span: dict, text: str) -> bool:
    """Append one span to *tw*. Returns False if the span should be skipped."""
    if not text.strip():
        return False
    bbox = pymupdf.Rect(span["bbox"])
    if bbox.is_empty or bbox.is_infinite:
        return False

    fontsize = float(span.get("size", 12))
    font = _pick_font(text)

    tl = font.text_length(text, fontsize)
    if tl > 0 and bbox.width > 0:
        fontsize = fontsize * bbox.width / tl
    fontsize = max(0.5, min(fontsize, 1000.0))

    descender = float(span.get("descender", -0.2))
    origin = (bbox.x0, bbox.y1 + descender * fontsize)

    try:
        tw.append(origin, text, font=font, fontsize=fontsize)
        return True
    except Exception as exc:
        print(f"[reinsert] skip {span.get('id', '?')}: {exc}")
        return False


def reinsert_text(
    outlined_pdf_path: str | Path,
    pages_data: dict,
    state: dict[str, str],          # flat {span_id: "keep"|"delete"}
    edited_texts: dict[str, str],   # {span_id: new_text}
    custom_spans: dict[str, dict],  # merged/custom spans keyed by span_id
    output_path: str | Path,
) -> dict[str, list[str]]:
    """Write kept spans back as invisible text over the outlined PDF.

    Returns {page_idx_str: [inserted_span_id, ...]} for round-trip verification.
    """
    doc = pymupdf.open(str(outlined_pdf_path))
    inserted: dict[str, list[str]] = {}

    for page_idx_str, page_data in pages_data.items():
        page_idx = int(page_idx_str)
        page = doc[page_idx]
        tw = pymupdf.TextWriter(page.rect)
        page_inserted: list[str] = []

        for span in page_data["spans"]:
            sid = span["id"]
            if state.get(sid, "delete") != "keep":
                continue
            text = edited_texts.get(sid) or span["text"]
            if _append_span(tw, span, text):
                page_inserted.append(sid)

        page_prefix = f"p{page_idx}_"
        for sid, span in custom_spans.items():
            if not sid.startswith(page_prefix):
                continue
            if state.get(sid, "delete") != "keep":
                continue
            text = edited_texts.get(sid) or span["text"]
            if _append_span(tw, span, text):
                page_inserted.append(sid)

        tw.write_text(page, render_mode=3)
        inserted[page_idx_str] = page_inserted

    doc.save(str(output_path), garbage=4, deflate=True, clean=True)
    doc.close()
    return inserted


def verify_roundtrip(output_path: str | Path, expected: dict[str, list[str]], pages_data: dict,
                     custom_spans: dict[str, dict] | None = None) -> list[str]:
    doc = pymupdf.open(str(output_path))
    issues: list[str] = []
    custom_spans = custom_spans or {}

    for page_idx_str, span_ids in expected.items():
        page_idx = int(page_idx_str)
        if page_idx >= len(doc):
            continue
        found_text = doc[page_idx].get_text("text")

        page_data = pages_data.get(page_idx_str, {})
        all_spans = {s["id"]: s for s in page_data.get("spans", [])}
        all_spans.update(custom_spans)

        for sid in span_ids:
            span = all_spans.get(sid)
            if not span:
                continue
            t = span["text"].strip()
            if t and t not in found_text:
                issues.append(f"p{page_idx_str} {sid!r}: '{t[:30]}' not in output")

    doc.close()
    return issues
