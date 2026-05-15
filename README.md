# PDF Text Sanitizer

A specialized tool for selectively rebuilding a PDF's searchable text layer. 

This application allows you to ingest a PDF, flatten its visible text into vector outlines (using Ghostscript), and then selectively re-insert specific text as an invisible, searchable layer (OCR-style). This is ideal for cleaning up poorly OCR'ed documents, removing sensitive information while keeping the layout pixel-perfect, or selectively enabling searchability on complex documents.

## Key Features

- **Pixel-Perfect Outlines:** Converts all visible text into vector shapes, ensuring the document looks exactly as intended without relying on installed fonts.
- **Selective Searchability:** Choose exactly which text remains searchable.
- **Interactive UI:** A keyboard-first web interface for reviewing, editing, and classifying text spans.
- **Regex-Powered Classification:** Batch-select text for keeping or deletion using regular expressions.
- **Non-Destructive Workflow:** The original PDF is never modified; a new, sanitized version is generated.

## How it Works

1. **Ingest:** Upload a PDF.
2. **Extract:** The tool extracts every text span and its precise coordinates.
3. **Outline:** Visible text is flattened to vector outlines via Ghostscript.
4. **Classify:** Use regex or the manual UI to mark spans as "keep" or "delete".
5. **Rebuild:** Only the "keep" spans are written back into the PDF as invisible text (render mode 3) layered perfectly over the outlines.
6. **Result:** A pixel-identical PDF where only your chosen text is searchable and selectable.

---

## Setup & Installation

### 1. Prerequisites (Ghostscript)
The `gs` command must be available in your system `PATH`.

- **macOS (Homebrew):** `brew install ghostscript`
- **Linux (apt):** `sudo apt install ghostscript`
- **Windows (winget):** `winget install ArtifexSoftware.Ghostscript`

### 2. Installation
Clone the repository and choose your preferred package manager:

```bash
git clone https://github.com/your-username/pdf-text-sanitizer.git
cd pdf-text-sanitizer
```

#### Option A: Using pip
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

#### Option B: Using [uv](https://github.com/astral-sh/uv) (Recommended)
```bash
uv venv
source .venv/bin/activate # On Windows: .venv\Scripts\activate
uv pip install -r requirements.txt
```

## Running the Application

### Easiest (recommended)

```bash
python start.py
```

Then open `http://127.0.0.1:8000`.

### Equivalent standard entrypoint

```bash
python -m app
```

### Optional run flags

Both commands support:

```bash
python start.py --host 0.0.0.0 --port 8000 --reload
python -m app --host 0.0.0.0 --port 8000 --reload
```

To disable reload:

```bash
python start.py --no-reload
```

### Legacy uvicorn command (still works)

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

After the server is running, drag and drop a PDF into the browser to begin processing.

---

## UI Keyboard Shortcuts

| Key | Action |
|---|---|
| `←` / `→` | Previous / next page |
| `g` | Jump to page |
| `Ctrl+A` | Select all spans on current page |
| `Delete` | Mark selected spans as **Delete** |
| `Enter` | Mark selected spans as **Keep** |
| `Ctrl+S` | Save and download the sanitized PDF |
| `Ctrl+Z` | Undo last action |
| `Double-click` | Inline edit span text (automatically marks as Keep) |

---

## License

MIT
