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

## Installation

### Prerequisites

- **Python 3.11+**
- **Ghostscript:** The `gs` command must be in your `PATH`.
  - **macOS:** `brew install ghostscript`
  - **Linux:** `sudo apt install ghostscript`
  - **Windows:** Download from [ghostscript.com](https://www.ghostscript.com/releases/) and ensure `gswin64c.exe` is renamed to `gs` or added to your PATH.

### Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/your-username/pdf-text-sanitizer.git
   cd pdf-text-sanitizer
   ```

2. Create a virtual environment and install dependencies:
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

## Usage

1. Start the FastAPI server:
   ```bash
   python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
   ```

2. Open your browser and go to `http://localhost:8000`.

3. Drag and drop your PDF to begin the sanitization process.

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
