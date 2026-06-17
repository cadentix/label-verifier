# TTB Alcohol Label Verifier

AI-powered COLA application label verification prototype for the TTB Compliance Division.

Agents extract form data and label images from uploaded PDFs, then use Claude's vision capability to verify that the label artwork matches the application — with intelligent judgment for case differences, punctuation variants, and OCR artefacts.

---

## Quick Start

### Prerequisites

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com)

### 1. Install dependencies

```bash
cd backend
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

### 3. Run

```bash
# From the project root
python backend/main.py
```

Open **http://localhost:8000** in your browser.

---

## Project Structure

```
ttb-label-verifier/
├── backend/
│   ├── main.py             # FastAPI server — all backend logic
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   └── index.html          # Single-page UI (served by backend)
├── agents/
│   └── agents.json         # Agent pipeline configuration / documentation
└── README.md
```

---

## How It Works

### Upload flow

1. User drops one or more COLA PDF files onto the browser UI.
2. Files are POSTed to `/verify`. The backend streams progress events (Server-Sent Events) back to the browser — no polling.

### Backend pipeline (per PDF)

| Step | Agent | What it does |
|------|-------|-------------|
| 1 | Form Extractor | Reads raw AcroForm fields or text from the PDF — no interpretation |
| 2 | Image Extractor | Pulls embedded images as raw bytes; renders page as PNG fallback |
| 3 | Claude Vision Verifier | Receives raw form data + images. Does all OCR, label reading, field comparison, and compliance judgment |

The server extracts and forwards. Claude does everything else.

### Fields verified

- Brand Name
- Class / Type Designation
- Alcohol Content (ABV)
- Net Contents
- Name and Address of Bottler / Producer
- Country of Origin (flagged N/A for domestic products)
- Government Health Warning Statement (exact per 27 CFR § 16.21)

### Matching logic

Claude applies judgment rather than simple string equality:

- `STONE'S THROW` and `Stone's Throw` → **PASS** (case is not a mismatch)
- `STONF.` and `STONE` → **PASS** (period as bottom of E — OCR artefact)
- Minor apostrophe/punctuation differences → **PASS**
- `Government Warning:` (title case) instead of `GOVERNMENT WARNING:` → **FAIL** (regulatory requirement)
- Different ABV number → **FAIL**

---

## Server Log

The UI includes a collapsible **Server Log** panel at the bottom that tails the backend's log stream in real time via `/logs` (SSE). Useful for seeing what was extracted and what was sent to Claude without needing terminal access.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serves the frontend |
| `GET` | `/health` | Health check + API key status |
| `POST` | `/verify` | Upload PDFs; returns SSE stream |
| `GET` | `/logs` | SSE stream of server logs |

---

## Dependencies

| Package | Why |
|---------|-----|
| `fastapi` + `uvicorn` | Web framework + ASGI server |
| `python-multipart` | Multipart file upload parsing |
| `pdfplumber` | Text extraction from PDFs |
| `PyMuPDF` | AcroForm field reading + image extraction + page rendering |
| `Pillow` | Image format conversion |
| `httpx` | Async HTTP client for Anthropic API |
| `python-dotenv` | `.env` file loading |

No ML frameworks, no heavyweight OCR stacks — kept small and auditable.

---

## Limitations & Notes

- **PDF quality matters**: If the label image is low-resolution or photographed at an angle, Claude flags it with `LOW` confidence and suggests `NEEDS_REVIEW`.
- **AcroForm dependency**: If the COLA PDF was printed and re-scanned rather than submitted electronically, form field extraction falls back to text heuristics which are less reliable.
- **No storage**: PDFs are processed in memory and discarded. No PII is persisted.
- **Rate limits**: Batch processing is sequential per file to avoid overwhelming the API. Parallel processing is possible with semaphore limiting if needed.

---

## Evaluation notes

> *"If we can't get results back in about 5 seconds, nobody's going to use it."* — Sarah Chen

Claude claude-sonnet-4-6 typically responds in 2–4 seconds for a single image. The SSE streaming means the user sees progress immediately — the UI updates field-by-field as results arrive rather than waiting for a full batch to complete.

> *"We need something my mother could figure out."* — Sarah Chen

The UI has exactly two interactions: drop files, click a button. Results are color-coded (green/red/amber) with plain-English findings in a table.

> *"There's nuance. You can't just pattern match everything."* — Dave Morrison

Claude is prompted to apply professional judgment, not regex. The system prompt explicitly lists known nuance cases (case differences, OCR artefacts, punctuation variants) and instructs Claude to treat them as matches.
