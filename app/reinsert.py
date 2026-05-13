"""Reinsert kept text as an invisible (render_mode=3) layer."""

from __future__ import annotations

from pathlib import Path

import pymupdf


# Base14 font used for Latin text — never embedded, zero size overhead
_LATIN_FONT = "helv"

# Fallback: Noto from pymupdf-fonts for CJK / non-Latin
_FALLBACK_FONT = "figo"  # NotoSans-Regular in pymupdf-fonts


def _pick_font(text: str) -> pymupdf.Font:
    """Choose the lightest font that can represent *text*."""
    try:
        font = pymupdf.Font(_LATIN_FONT)
        # If any glyph is missing (glyph id 0) for non-space chars, fall back
        if all(font.has_glyph(ord(ch)) for ch in text if not ch.isspace()):
            return font
    except Exception:
        pass
    try:
        return pymupdf.Font(_FALLBACK_FONT)
    except Exception:
        return pymupdf.Font(_LATIN_FONT)


def _effective(span_id: str, auto: dict[str, str], overrides: dict[str, str]) -> str:
    return overrides.get(span_id) or auto.get(span_id, "keep")


def reinsert_text(
    outlined_pdf_path: str | Path,
    pages_data: dict,
    auto_classifications: dict[str, dict[str, str]],
    overrides: dict[str, str],
    edited_texts: dict[str, str],
    output_path: str | Path,
) -> dict[str, list[str]]:
    """Write kept spans back as invisible text over the outlined PDF.

    Returns a dict ``{page_idx_str: [span_id, ...]}`` of inserted spans for
    round-trip verification.
    """
    doc = pymupdf.open(str(outlined_pdf_path))
    inserted: dict[str, list[str]] = {}

    for page_idx_str, page_data in pages_data.items():
        page_idx = int(page_idx_str)
        page = doc[page_idx]
        auto_page = auto_classifications.get(page_idx_str, {})

        tw = pymupdf.TextWriter(page.rect)
        page_inserted: list[str] = []

        for span in page_data["spans"]:
            sid = span["id"]
            if _effective(sid, auto_page, overrides) != "keep":
                continue

            text = edited_texts.get(sid) or span["text"]
            if not text.strip():
                continue

            bbox = pymupdf.Rect(span["bbox"])
            if bbox.is_empty or bbox.is_infinite:
                continue

            fontsize = float(span["size"])
            font = _pick_font(text)

            # Scale fontsize so text_length ≈ bbox.width  (Tesseract trick)
            tl = font.text_length(text, fontsize)
            if tl > 0 and bbox.width > 0:
                fontsize = fontsize * bbox.width / tl
            # Guard against degenerate scaling
            fontsize = max(0.5, min(fontsize, 1000.0))

            # Baseline origin in PyMuPDF y-down space:
            # bbox.y1 is the bottom edge; descender < 0 moves up into the bbox.
            descender = float(span.get("descender", -0.2))
            origin = (bbox.x0, bbox.y1 + descender * fontsize)

            try:
                tw.append(origin, text, font=font, fontsize=fontsize)
                page_inserted.append(sid)
            except Exception as exc:
                print(f"[reinsert] skip {sid}: {exc}")

        tw.write_text(page, render_mode=3)
        inserted[page_idx_str] = page_inserted

    doc.save(
        str(output_path),
        garbage=4,
        deflate=True,
        clean=True,
    )
    doc.close()
    return inserted


def verify_roundtrip(output_path: str | Path, expected: dict[str, list[str]], pages_data: dict) -> list[str]:
    """Compare extracted text from *output_path* against the kept set.

    Returns list of mismatch descriptions (empty = all good).
    """
    doc = pymupdf.open(str(output_path))
    issues: list[str] = []

    for page_idx_str, span_ids in expected.items():
        page_idx = int(page_idx_str)
        if page_idx >= len(doc):
            continue
        page = doc[page_idx]
        found_text = page.get_text("text")

        page_data = pages_data.get(page_idx_str, {})
        for sid in span_ids:
            span = next((s for s in page_data.get("spans", []) if s["id"] == sid), None)
            if span is None:
                continue
            if span["text"].strip() and span["text"].strip() not in found_text:
                issues.append(f"p{page_idx_str} {sid!r}: text not found in output")

    doc.close()
    return issues
