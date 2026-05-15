"""Regex-based pre-classification of text spans."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


# ── Pattern compilation ────────────────────────────────────────────────────────

def build_flags(case_insensitive: bool = False, multiline: bool = False, dotall: bool = False) -> int:
    f = 0
    if case_insensitive:
        f |= re.IGNORECASE
    if multiline:
        f |= re.MULTILINE
    if dotall:
        f |= re.DOTALL
    return f


def compile_patterns(patterns: list[str], flags: int = 0) -> list[re.Pattern]:
    """Compile patterns, skipping any that are invalid."""
    compiled: list[re.Pattern] = []
    for p in patterns:
        if not p.strip():
            continue
        try:
            compiled.append(re.compile(p, flags))
        except re.error:
            pass  # caller should validate separately for user feedback
    return compiled


def validate_patterns(patterns: list[str], flags: int = 0) -> list[str]:
    """Return a list of error strings (one per bad pattern, empty = all ok)."""
    errors: list[str] = []
    for p in patterns:
        if not p.strip():
            continue
        try:
            re.compile(p, flags)
        except re.error as exc:
            errors.append(f"/{p}/: {exc}")
    return errors


# ── Classification ─────────────────────────────────────────────────────────────

def _matches_any(text: str, patterns: list[re.Pattern]) -> bool:
    return any(p.search(text) for p in patterns)


def classify_span(span: dict, patterns: list[re.Pattern], granularity: str = "span") -> str:
    """Return 'keep' or 'delete' for a single span dict."""
    if not patterns:
        return "keep"

    if granularity == "word":
        words = span["text"].split()
        return "keep" if any(_matches_any(w, patterns) for w in words) else "delete"

    return "keep" if _matches_any(span["text"], patterns) else "delete"


def split_span_by_regex(span: dict, pattern: re.Pattern) -> list[dict]:
    """Split a span's text into multiple parts based on regex matches.
    Returns a list of 'part' dicts, each with 'text' and 'is_match'.
    The coordinates (bbox) for these parts aren't calculated here (requires font metrics).
    """
    text = span["text"]
    parts = []
    last_end = 0
    for match in pattern.finditer(text):
        start, end = match.span()
        if start > last_end:
            parts.append({"text": text[last_end:start], "is_match": False})
        parts.append({"text": text[start:end], "is_match": True})
        last_end = end
    if last_end < len(text):
        parts.append({"text": text[last_end:], "is_match": False})
    return parts


def apply_classification(
    pages_data: dict,
    patterns: list[str],
    flags: int = 0,
    granularity: str = "span",
) -> dict[str, dict[str, str]]:
    """Classify every span in *pages_data*.

    Returns ``{page_idx_str: {span_id: "keep"|"delete"}}``
    with no regard for existing manual overrides — that merging
    is the caller's responsibility.
    """
    compiled = compile_patterns(patterns, flags)
    result: dict[str, dict[str, str]] = {}

    for page_idx, page in pages_data.items():
        page_result: dict[str, str] = {}
        for span in page["spans"]:
            page_result[span["id"]] = classify_span(span, compiled, granularity)
        result[page_idx] = page_result

    return result


# ── Preset persistence ─────────────────────────────────────────────────────────

class PresetStore:
    """Simple JSON-backed named regex preset store."""

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "presets.json"
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")

    def list(self) -> list[str]:
        return sorted(self._data.keys())

    def get(self, name: str) -> dict | None:
        return self._data.get(name)

    def save(self, name: str, preset: dict) -> None:
        self._data[name] = preset
        self._save()

    def delete(self, name: str) -> bool:
        if name in self._data:
            del self._data[name]
            self._save()
            return True
        return False

    def all(self) -> dict:
        return dict(self._data)
