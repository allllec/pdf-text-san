"""Convert a PDF's text to vector outlines via Ghostscript."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pymupdf


def _gs_executable() -> str:
    for name in ("gswin64c", "gswin32c", "gs"):
        if shutil.which(name):
            return name
    raise FileNotFoundError(
        "Ghostscript not found. Install it and ensure gswin64c (Windows) or gs "
        "(Linux/macOS) is on PATH."
    )


def outline_text(input_path: str | Path, output_path: str | Path) -> None:
    """Run Ghostscript to flatten all text to vector outlines.

    Raises RuntimeError if GS fails or the page geometry changes.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    gs = _gs_executable()
    cmd = [
        gs,
        "-dNoOutputFonts",
        "-dNOPAUSE",
        "-dBATCH",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.7",
        f"-sOutputFile={output_path}",
        str(input_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Ghostscript failed (exit {result.returncode}):\n{result.stderr}")

    _verify_geometry(input_path, output_path)


def _verify_geometry(orig_path: Path, outlined_path: Path) -> list[str]:
    """Check page count and rect equality; return list of warning strings."""
    warnings: list[str] = []
    orig = pymupdf.open(str(orig_path))
    outlined = pymupdf.open(str(outlined_path))

    if len(orig) != len(outlined):
        orig.close()
        outlined.close()
        raise RuntimeError(
            f"Page count changed after Ghostscript pass: "
            f"{len(orig)} → {len(outlined)}"
        )

    for i in range(len(orig)):
        op = orig[i]
        np_ = outlined[i]
        # Allow 1pt tolerance for floating-point differences
        if abs(op.rect.width - np_.rect.width) > 1 or abs(op.rect.height - np_.rect.height) > 1:
            warnings.append(
                f"Page {i}: rect changed {op.rect} → {np_.rect}; "
                "coordinates may drift during reinsertion."
            )

    orig.close()
    outlined.close()
    return warnings
