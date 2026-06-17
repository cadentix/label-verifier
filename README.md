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

## Evaluation notes (assumptions and approach)

> *"If we can't get results back in about 5 seconds, nobody's going to use it."* — Sarah Chen

Claude claude-sonnet-4-6 typically responds in 2–4 seconds for a single image. The SSE streaming means the user sees progress immediately — the UI updates field-by-field as results arrive rather than waiting for a full batch to complete.

> *"We need something my mother could figure out."* — Sarah Chen

The UI has exactly two interactions: drop files, click a button. Results are color-coded (green/red/amber) with plain-English findings in a table.

> *"There's nuance. You can't just pattern match everything."* — Dave Morrison

Claude is prompted to apply professional judgment, not regex. The system prompt explicitly lists known nuance cases (case differences, OCR artefacts, punctuation variants) and instructs Claude to treat them as matches.

> *"Think of this as a standalone proof-of-concept that could potentially inform future procurement decisions"* - Marcus Williams

The project's structure might feel flat, and that's because it is. The system prompt suggest that this is supposed to be a quick solution to show what is possible. If more interest is shared with it, more time can be spent on making it more maintable.

> *"We encourage you to review TTB's guidelines at ttb.gov for additional context on label requirements."*

This was done during testing and there was a significant delay in how quickly claude handled the request. the time it took to get a FULL report on the entire PDF was over 20 seconds. Additionally, PII was being sent to claude. This solution only focuses on a small subset things that AI would be good at.

> *"... ther's PII considerations, document retention policies, the usual federal complience stuff. But for the prototype? Just don't do anything crazy."

The PDF actually gets stripped of its contents. This makes flattened PDFs, "print to pdf", unable to reliably extract the text. For these, the system forwards the entire document to Claude which is more expensive and has PII concerns but that can be addressed later after the prototyping phase.

### Trade offs

I did not see the take home assignment until the 14th. It was sent on the 9th with 1 week given to complete. I made plenty of mistakes along the way. I'm hoping my submission is still allowed to be modified the day after the deadline through git commits without flagging anything.

Hosting was probably the biggest hurdle for this because none of the free options worked - they would either spin down from non-use or I didn't have shell access to troubleshoot their build process.

UI is as barebones as it gets. The system did not call for a state machine and the project didn't need one.


