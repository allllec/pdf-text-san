"""Extract text spans from a PDF using PyMuPDF rawdict."""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf


def extract_text(pdf_path: str | Path) -> dict:
    """Return per-page span data extracted from *pdf_path*.

    Returns a dict keyed by str(page_index) where each value is::

        {
          "spans": [...],
          "words": [...],
          "page_rect": [x0, y0, x1, y1],
          "page_width": float,
          "page_height": float,
        }

    Each span::

        {
          "id": "p0_s3",
          "text": "Hello",
          "bbox": [x0, y0, x1, y1],   # PyMuPDF device space (y-down)
          "origin": [x, y],
          "font": "Helvetica",
          "size": 12.0,
          "color": 0,
          "dir": [1.0, 0.0],
          "flags": int,
          "ascender": float,
          "descender": float,          # negative
          "chars": [{"c": str, "bbox": [...], "origin": [...]}],
        }
    """
    doc = pymupdf.open(str(pdf_path))
    pages_data: dict = {}

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        rawdict = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_TEXT)
        words = page.get_text("words")  # (x0,y0,x1,y1,word,block,line,word)

        spans: list[dict] = []
        span_id = 0

        for block in rawdict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    if not text:
                        continue
                    spans.append(
                        {
                            "id": f"p{page_idx}_s{span_id}",
                            "text": text,
                            "bbox": list(span["bbox"]),
                            "origin": list(span["origin"]),
                            "font": span.get("font", ""),
                            "size": float(span.get("size", 12)),
                            "color": span.get("color", 0),
                            "dir": list(span.get("dir", (1, 0))),
                            "flags": span.get("flags", 0),
                            "ascender": float(span.get("ascender", 0.8)),
                            "descender": float(span.get("descender", -0.2)),
                            "chars": [
                                {
                                    "c": ch.get("c", ""),
                                    "bbox": list(ch.get("bbox", span["bbox"])),
                                    "origin": list(ch.get("origin", span["origin"])),
                                }
                                for ch in span.get("chars", [])
                            ],
                        }
                    )
                    span_id += 1

        pages_data[str(page_idx)] = {
            "spans": spans,
            "words": [list(w) for w in words],
            "page_rect": list(page.rect),
            "page_width": float(page.rect.width),
            "page_height": float(page.rect.height),
        }

    doc.close()
    return pages_data


def save_sidecar(pages_data: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(pages_data, ensure_ascii=False), encoding="utf-8")


def load_sidecar(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
