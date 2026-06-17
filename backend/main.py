"""
TTB Alcohol Label Verification Backend
FastAPI server with SSE progress streaming and Claude-powered label analysis.
"""

import asyncio
import base64
import io
import json
import logging
import os
import sys
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

import httpx
import fitz  # PyMuPDF
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

# ── Logging setup ─────────────────────────────────────────────────────────────

LOG_BUFFER_SIZE = 500
log_buffer: deque[dict] = deque(maxlen=LOG_BUFFER_SIZE)
log_subscribers: list[asyncio.Queue] = []


class BufferedHandler(logging.Handler):
    """Captures log records into an in-memory buffer and notifies SSE subscribers."""

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "level": record.levelname,
            "msg": self.format(record),
        }
        log_buffer.append(entry)
        for q in list(log_subscribers):
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass


_handler = BufferedHandler()
_handler.setFormatter(logging.Formatter("%(message)s"))

logger = logging.getLogger("ttb")
logger.setLevel(logging.DEBUG)
logger.addHandler(_handler)

# Also mirror to stdout
_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(_stdout_handler)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("TTB Label Verifier backend starting up")
    yield
    logger.info("TTB Label Verifier backend shutting down")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="TTB Label Verifier", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def _find_project_root() -> Path:
    """
    Locate the project root regardless of whether main.py sits flat
    (e.g. /app/main.py in the Docker container) or nested (backend/main.py
    when run locally). Walks up from this file looking for the agents/
    and frontend/ directories.
    """
    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent):
        if (candidate / "agents").exists() and (candidate / "frontend").exists():
            return candidate
    # Fall back to the file's own directory if neither match
    return here

PROJECT_ROOT = _find_project_root()
FRONTEND_DIR = PROJECT_ROOT / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_AGENT_ID = os.getenv("AGENT", "")
ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION  = "2023-06-01"

# Tool schema — loaded once from agents.json at startup
_AGENTS_JSON = PROJECT_ROOT / "agents" / "agents.json"
with open(_AGENTS_JSON) as _f:
    _AGENT_DEF = json.load(_f)

# Extract just the tools array from the agent definition for use in /v1/messages
# agents.json is a Managed Agents object; the tools list is under "tools"
# but /v1/messages needs each tool without the "type": "custom" wrapper
_RAW_TOOLS = _AGENT_DEF.get("tools", [])
VERIFICATION_TOOLS = [
    {
        "name": t["name"],
        "description": t["description"],
        "input_schema": t["input_schema"],
    }
    for t in _RAW_TOOLS
    if t.get("type") == "custom"
]

VERIFICATION_SYSTEM = _AGENT_DEF.get("system", "")

# ── PDF extraction ─────────────────────────────────────────────────────────────


def extract_label_images(pdf_bytes: bytes) -> list[dict]:
    """
    Extract embedded images from a PDF using fitz (PyMuPDF).
    Any format fitz can't serve directly is re-rendered via its own pixmap —
    no Pillow needed. Skips images smaller than 100x100px.
    """
    images = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page_num, page in enumerate(doc):
        for img_ref in page.get_images(full=True):
            xref = img_ref[0]
            base_image = doc.extract_image(xref)
            img_bytes = base_image["image"]
            ext = base_image["ext"]
            w, h = base_image["width"], base_image["height"]
            if w < 100 or h < 100:
                continue
            # Normalise to PNG for anything Claude might not accept natively
            if ext in ("png", "jpg", "jpeg"):
                media_type = "image/jpeg" if ext == "jpg" else f"image/{ext}"
            else:
                # Re-render through fitz pixmap — no external library needed
                pix = fitz.Pixmap(doc, xref)
                if pix.alpha:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                img_bytes = pix.tobytes("png")
                media_type = "image/png"
            images.append({
                "data": base64.standard_b64encode(img_bytes).decode(),
                "media_type": media_type,
                "page": page_num + 1,
                "width": w,
                "height": h,
            })
    doc.close()
    images.sort(key=lambda x: x["width"] * x["height"], reverse=True)
    return images


def render_page_as_image(pdf_bytes: bytes, page_num: int = 0) -> dict | None:
    """
    Render a PDF page to an image (fallback when no embedded images found).
    """
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page = doc[min(page_num, len(doc) - 1)]
        mat = fitz.Matrix(2.0, 2.0)  # 2x resolution
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
        doc.close()
        return {
            "data": base64.standard_b64encode(img_bytes).decode(),
            "media_type": "image/png",
            "page": page_num + 1,
            "width": pix.width,
            "height": pix.height,
        }
    except Exception as e:
        logger.warning(f"Page render failed: {e}")
        return None


# ── Claude API ────────────────────────────────────────────────────────────────

async def verify_label_with_claude(
    pdf_bytes: bytes,
    images: list[dict],
) -> dict:
    """
    POST to /v1/messages with the tool schema from agents.json.
    Forces the report_verification_results tool call — result comes back
    as a parsed dict directly from the tool_use block, no JSON cleanup needed.

    The whole PDF is always sent as a document block, alongside the extracted
    label image(s). Claude reads the COLA application data directly from the
    PDF and reads the label from the image — no local form-field extraction
    or text parsing happens on the server at all.
    """
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not configured")

    content: list[dict] = [{
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.standard_b64encode(pdf_bytes).decode(),
        },
    }]

    for img in images[:3]:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img["media_type"],
                "data": img["data"],
            },
        })

    content.append({
        "type": "text",
        "text": (
            "The attached PDF is a COLA application (TTB Form 5100.31). Read the "
            "application data directly from the PDF, and examine the label "
            "image(s) provided. Call report_verification_results with your "
            "complete findings."
        ),
    })

    payload = {
        "model": _AGENT_DEF.get("model", "claude-sonnet-4-6"),
        "max_tokens": 1500,
        "system": VERIFICATION_SYSTEM,
        "tools": VERIFICATION_TOOLS,
        "tool_choice": {"type": "tool", "name": "report_verification_results"},
        "messages": [{"role": "user", "content": content}],
    }

    logger.info(f"Sending to agent {ANTHROPIC_AGENT_ID}: full PDF + {len(images)} image(s)")

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            ANTHROPIC_ENDPOINT,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            content=json.dumps(payload),
        )
        resp.raise_for_status()
        data = resp.json()

    tool_block = next(
        (b for b in data.get("content", []) if b.get("type") == "tool_use"),
        None,
    )

    if tool_block is None:
        logger.error("No tool_use block in Claude response")
        return {
            "overall_status": "NEEDS_REVIEW",
            "summary": "Claude did not return a structured result",
            "fields": {},
            "additional_issues": ["report_verification_results was not called"],
            "confidence": "LOW",
        }

    result: dict = tool_block["input"]
    logger.info(
        f"Claude result: {result.get('overall_status')} "
        f"(confidence: {result.get('confidence')}) — {result.get('summary', '')}"
    )
    return result


# ── Processing pipeline ───────────────────────────────────────────────────────


async def process_single_pdf(
    filename: str,
    pdf_bytes: bytes,
    progress_cb,
) -> dict:
    """
    Full pipeline for one PDF:
    1. Extract label image(s) from PDF (raw bytes only — no analysis)
    2. Send the whole PDF + image(s) to Claude — all reading, OCR, form
       interpretation, and compliance judgment happens there. The server
       does no text or field extraction of its own.
    """
    job_id = str(uuid.uuid4())[:8]
    logger.info(f"[{job_id}] Processing: {filename}")

    await progress_cb({"stage": "imaging", "file": filename, "pct": 30})

    # 1. Extract label images (raw bytes only — no analysis)
    try:
        images = extract_label_images(pdf_bytes)
        logger.info(f"[{job_id}] Extracted {len(images)} image(s) from PDF")
    except Exception as e:
        logger.error(f"[{job_id}] Image extraction failed: {e}")
        images = []

    # Fallback: render the first page as an image if no embedded images found
    if not images:
        logger.info(f"[{job_id}] No embedded images; rendering page 1 as fallback")
        rendered = render_page_as_image(pdf_bytes, 0)
        if rendered:
            images = [rendered]

    await progress_cb({"stage": "verifying", "file": filename, "pct": 60})

    # 2. Claude does all the work: reads the PDF, reads the label, OCRs text,
    # checks every field. The whole PDF is always forwarded.
    claude_result: dict = {}
    if ANTHROPIC_API_KEY:
        try:
            claude_result = await verify_label_with_claude(pdf_bytes, images)
        except Exception as e:
            logger.error(f"[{job_id}] Claude API error: {e}")
            claude_result = {
                "overall_status": "NEEDS_REVIEW",
                "summary": f"Claude API error: {e}",
                "fields": {},
                "additional_issues": [],
                "confidence": "LOW",
            }
    else:
        logger.warning(f"[{job_id}] ANTHROPIC_API_KEY not set")
        claude_result = {
            "overall_status": "NEEDS_REVIEW",
            "summary": "Verification skipped — ANTHROPIC_API_KEY not set",
            "fields": {},
            "additional_issues": ["Set ANTHROPIC_API_KEY in .env to enable verification"],
            "confidence": "LOW",
        }

    await progress_cb({"stage": "complete", "file": filename, "pct": 100})

    return {
        "job_id": job_id,
        "filename": filename,
        "images_found": len(images),
        "result": claude_result,
        "processed_at": datetime.utcnow().isoformat() + "Z",
    }


# ── SSE helpers ───────────────────────────────────────────────────────────────


def sse_event(data: dict, event: str = "message") -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return HTMLResponse(content=index.read_text())
    return HTMLResponse("<h1>TTB Label Verifier</h1><p>Frontend not found.</p>")


@app.get("/health")
async def health():
    return {"status": "ok", "agent_id": ANTHROPIC_AGENT_ID or "not set", "api_key_set": bool(ANTHROPIC_API_KEY), "model": _AGENT_DEF.get("model", "not set")}


@app.post("/verify")
async def verify_labels(files: list[UploadFile] = File(...)):
    """
    Accept one or more PDF files. Returns a streaming SSE response with per-file
    progress events followed by result events.
    """
    if not files:
        raise HTTPException(400, "No files uploaded")

    # Read all files upfront (can't stream from upload inside SSE generator)
    file_data: list[tuple[str, bytes]] = []
    for f in files:
        if not f.filename or not f.filename.lower().endswith(".pdf"):
            continue
        content = await f.read()
        file_data.append((f.filename, content))

    if not file_data:
        raise HTTPException(400, "No valid PDF files found")

    logger.info(f"Batch received: {len(file_data)} file(s)")

    async def event_stream() -> AsyncGenerator[str, None]:
        yield sse_event({"type": "batch_start", "total": len(file_data)}, "batch_start")

        for i, (fname, fbytes) in enumerate(file_data):
            events: asyncio.Queue = asyncio.Queue(maxsize=50)

            async def progress_cb(data: dict):
                await events.put(data)

            # Start processing in background
            task = asyncio.create_task(process_single_pdf(fname, fbytes, progress_cb))

            # Stream progress until done
            while not task.done():
                try:
                    prog = await asyncio.wait_for(events.get(), timeout=0.2)
                    yield sse_event(
                        {"type": "progress", "file": fname, "index": i, **prog},
                        "progress",
                    )
                except asyncio.TimeoutError:
                    pass

            # Drain remaining progress events
            while not events.empty():
                prog = events.get_nowait()
                yield sse_event(
                    {"type": "progress", "file": fname, "index": i, **prog},
                    "progress",
                )

            try:
                file_result = task.result()
                yield sse_event(
                    {"type": "file_result", "index": i, **file_result},
                    "file_result",
                )
            except Exception as e:
                logger.error(f"Task error for {fname}: {e}")
                yield sse_event(
                    {
                        "type": "file_error",
                        "index": i,
                        "filename": fname,
                        "error": str(e),
                    },
                    "file_error",
                )

        yield sse_event({"type": "batch_complete"}, "batch_complete")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/logs")
async def stream_logs():
    """
    SSE endpoint that streams server log entries in real time.
    Sends buffered history first, then live updates.
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    log_subscribers.append(q)

    async def log_stream() -> AsyncGenerator[str, None]:
        # Send history
        for entry in list(log_buffer):
            yield f"data: {json.dumps(entry)}\n\n"

        # Live tail
        try:
            while True:
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(entry)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            log_subscribers.remove(q)

    return StreamingResponse(
        log_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, log_level="info")
