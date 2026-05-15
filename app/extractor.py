"""Extract text items from a PDF.

Primary unit: individual words from page.get_text("words") — these are always
populated, even when rawdict spans are empty (e.g. certain font types / encodings).
Rawdict is used only as a secondary source for font / size / color metadata.
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf


# ── Metadata lookup helpers ────────────────────────────────────────────────────

def _build_meta_index(textdict: dict) -> list[dict]:
    """Collect span metadata from a get_text('dict') result for spatial lookup."""
    meta: list[dict] = []
    for block in textdict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                meta.append(
                    {
                        "bbox": span["bbox"],
                        "font": span.get("font", ""),
                        "size": float(span.get("size", 12)),
                        "color": span.get("color", 0),
                        "dir": list(span.get("dir", (1, 0))),
                        "flags": span.get("flags", 0),
                        "ascender": float(span.get("ascender", 0.8)),
                        "descender": float(span.get("descender", -0.2)),
                    }
                )
    return meta


_FALLBACK_META = {
    "font": "", "size": 12.0, "color": 0,
    "dir": [1, 0], "flags": 0, "ascender": 0.8, "descender": -0.2,
}


def _lookup_meta(cx: float, cy: float, meta_index: list[dict]) -> dict:
    """Return metadata for the rawdict span that contains (cx, cy), or nearest."""
    # First pass: exact containment
    for m in meta_index:
        x0, y0, x1, y1 = m["bbox"]
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return m
    # Second pass: closest centroid (handles slight bbox mismatches)
    best, best_d = None, float("inf")
    for m in meta_index:
        x0, y0, x1, y1 = m["bbox"]
        d = ((x0 + x1) / 2 - cx) ** 2 + ((y0 + y1) / 2 - cy) ** 2
        if d < best_d:
            best_d, best = d, m
    return best or _FALLBACK_META


# ── Main extraction ────────────────────────────────────────────────────────────

def extract_text(pdf_path: str | Path) -> dict:
    """Return per-page word-level items extracted from *pdf_path*.

    Words from ``page.get_text("words")`` are always the primary unit because
    they are reliably populated regardless of PDF encoding.  ``get_text("dict")``
    (with flags=0 to avoid clipping) provides font / size / color metadata per span.

    Returns a dict keyed by str(page_index)::

        {
          "spans": [          # one entry per word
            {
              "id": "p0_w3",
              "text": "Hello",
              "bbox": [x0, y0, x1, y1],
              "origin": [x0, y1],        # bottom-left (used for reinsertion)
              "font": str,
              "size": float,
              "color": int,
              "dir": [float, float],
              "flags": int,
              "ascender": float,
              "descender": float,        # negative
            }, ...
          ],
          "words":      [...],           # raw word tuples (kept for reference)
          "page_rect":  [x0, y0, x1, y1],
          "page_width":  float,
          "page_height": float,
        }
    """
    doc = pymupdf.open(str(pdf_path))
    pages_data: dict = {}

    for page_idx in range(len(doc)):
        page = doc[page_idx]

        # "dict" gives block/line/span structure with font+size+color metadata.
        # flags=0 disables clipping/filtering that can silently drop spans.
        textdict = page.get_text("dict", flags=0)
        meta_index = _build_meta_index(textdict)

        # Words are the authoritative text source
        words = page.get_text("words")  # (x0,y0,x1,y1, text, block, line, word_no)

        spans: list[dict] = []
        for word_idx, w in enumerate(words):
            x0, y0, x1, y1, text = float(w[0]), float(w[1]), float(w[2]), float(w[3]), w[4]
            if not text.strip():
                continue
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            meta = _lookup_meta(cx, cy, meta_index)
            spans.append(
                {
                    "id": f"p{page_idx}_w{word_idx}",
                    "text": text,
                    "bbox": [x0, y0, x1, y1],
                    "origin": [x0, y1],
                    "font":      meta["font"],
                    "size":      meta["size"],
                    "color":     meta["color"],
                    "dir":       meta["dir"],
                    "flags":     meta["flags"],
                    "ascender":  meta["ascender"],
                    "descender": meta["descender"],
                }
            )

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
