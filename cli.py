#!/usr/bin/env python3
"""CLI entry point — headless batch processing.

Usage examples::

    python cli.py input.pdf -r "\d{4}-\d{4}" -o output.pdf
    python cli.py input.pdf -r "INV-\w+" -r "\d{4}" -o out.pdf --verify
    python cli.py input.pdf --preset invoices -o out.pdf
    python cli.py input.pdf -r ".*" -o all.pdf  # keep everything
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="pdfrebuild",
        description="Selectively rebuild a PDF's searchable text layer.",
    )
    parser.add_argument("input", type=Path, help="Input PDF path")
    parser.add_argument("-o", "--output", type=Path, default=None, help="Output PDF path (default: <input>_rebuilt.pdf)")
    parser.add_argument("-r", "--regex", dest="patterns", action="append", default=[], metavar="PATTERN",
                        help="Regex pattern to keep (repeatable; OR logic)")
    parser.add_argument("--preset", default=None, metavar="NAME",
                        help="Load named regex preset instead of -r patterns")
    parser.add_argument("-i", "--ignore-case", action="store_true", help="Case-insensitive matching")
    parser.add_argument("-m", "--multiline",   action="store_true", help="Multiline flag")
    parser.add_argument("-s", "--dotall",      action="store_true", help="Dot-all flag")
    parser.add_argument("--granularity", choices=["span", "word"], default="span",
                        help="Match granularity (default: span)")
    parser.add_argument("--selection", type=Path, default=None, metavar="JSON",
                        help="Override JSON file (manual span selection)")
    parser.add_argument("--work-dir", type=Path, default=None,
                        help="Working directory for intermediate files")
    parser.add_argument("--no-verify", action="store_true", help="Skip round-trip verification")
    parser.add_argument("--preset-dir", type=Path, default=None,
                        help="Directory for preset JSON file (default: ~/.pdfsan/presets)")

    args = parser.parse_args()

    if not args.input.exists():
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return 1

    output = args.output or args.input.with_name(args.input.stem + "_rebuilt.pdf")

    # Import here so the module isn't required if someone just does --help
    import re
    from app.classifier import build_flags
    from app.pipeline import run_headless

    flags = build_flags(args.ignore_case, args.multiline, args.dotall)

    try:
        summary = run_headless(
            input_pdf=args.input,
            output_pdf=output,
            patterns=args.patterns or None,
            flags=flags,
            granularity=args.granularity,
            preset_name=args.preset,
            preset_dir=args.preset_dir,
            selection_json=args.selection,
            work_dir=args.work_dir,
            verify=not args.no_verify,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    if summary["issues"]:
        print(f"Completed with {len(summary['issues'])} round-trip warning(s).")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
