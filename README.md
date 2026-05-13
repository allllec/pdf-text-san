# PDF Text Sanitiser

A keyboard-first local tool that selectively rebuilds a PDF's searchable text layer.

**Workflow:** ingest PDF → extract all text spans → flatten visible text to vector outlines via Ghostscript → regex pre-classify every span (keep / delete) → interactive UI to review and adjust → write only the kept text back as invisible (render mode 3) over the outlines → save a pixel-identical PDF where only the chosen text is searchable.

---

## Requirements

| Dependency | Notes |
|---|---|
| Python 3.11+ | [python.org](https://www.python.org/downloads/) |
| Ghostscript | `gswin64c.exe` must be on `PATH`. Download from [ghostscript.com](https://www.ghostscript.com/releases/) |
| PyMuPDF, FastAPI, etc. | Installed automatically by `run.bat` or `pip install -r requirements.txt` |

---

## Quick start (Windows)

```bat
run.bat
```

This installs dependencies, starts the local server on `http://localhost:8000`, and opens your browser.

### Linux / macOS

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Then open `http://localhost:8000`.

---

## CLI — headless batch mode

Full auto, no UI:

```bash
python cli.py input.pdf --regex "\d{4}-\d{4}" -o output.pdf
```

Multiple patterns (OR logic):

```bash
python cli.py invoice.pdf -r "INV-\w+" -r "\d{4}-\d{2}-\d{2}" -o result.pdf
```

Case-insensitive + per-word granularity:

```bash
python cli.py report.pdf -r "total" -i --granularity word -o report_out.pdf
```

Load a named preset saved from the UI:

```bash
python cli.py scan.pdf --preset invoices -o scan_out.pdf
```

Supply a manual selection JSON (overrides):

```bash
python cli.py doc.pdf --selection selection.json -o doc_out.pdf
```

### Batch folder

```bash
for f in /docs/*.pdf; do
    python cli.py "$f" -r "\d{4}-\d{4}" -o "/out/$(basename $f)"
done
```

---

## Importable pipeline

```python
from app.pipeline import run_headless

summary = run_headless(
    input_pdf="invoice.pdf",
    output_pdf="invoice_rebuilt.pdf",
    patterns=[r"\d{4}-\d{4}", r"INV-\w+"],
)
print(f"Kept {summary['kept']} of {summary['total']} spans")
```

---

## UI keyboard shortcuts

| Key | Action |
|---|---|
| `←` / `→` | Previous / next page |
| `PgUp` / `PgDn` | Previous / next page |
| `g` | Jump to page (focuses page number input) |
| `Ctrl+Wheel` | Zoom in / out |
| `Ctrl+A` | Select all spans on current page |
| `Esc` | Clear selection (or cancel inline edit) |
| `Delete` | Set selected spans → delete |
| `Enter` | Set selected spans → keep |
| `Ctrl+S` | Save and download output PDF |
| `Ctrl+Z` | Undo last toggle batch |
| `Ctrl+Enter` | Apply regex immediately |
| **Click** | Toggle / select single span |
| **Drag** | Rubber-band select; toggles all intersecting spans |
| **Shift+drag** | Force-keep all intersecting spans |
| **Alt+drag** | Force-delete all intersecting spans |
| **Double-click** | Inline edit span text (implies keep) |

---

## Colour coding

| Colour | Meaning |
|---|---|
| Green | Will be kept and reinserted as invisible searchable text |
| Red | Will be discarded |
| Yellow/orange | Manually overridden from the auto-classified state |

---

## Example regex patterns

| Pattern | Use case |
|---|---|
| `\d{4}-\d{4}` | Four-digit codes like account numbers |
| `INV-\w+` | Invoice identifiers |
| `\b[A-Z]{2}\d{6}\b` | Passport / ID numbers |
| `\d{1,3}[.,]\d{2}` | Decimal amounts |
| `\b[A-Z][a-z]+ [A-Z][a-z]+\b` | Two-word proper names |

---

## Architecture

```
app/
├── extractor.py   – PyMuPDF rawdict text extraction → JSON sidecar
├── outliner.py    – Ghostscript text-to-outlines pass
├── classifier.py  – Regex pre-classification + preset store
├── reinsert.py    – Invisible text reinsertion (render_mode=3)
├── pipeline.py    – Headless pipeline (importable)
├── main.py        – FastAPI server + session state
└── static/        – Vanilla JS single-page UI
cli.py             – CLI entry point
run.bat            – Windows one-click launcher
```
