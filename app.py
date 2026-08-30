"""FastAPI web service: recover readable text from a garbled/mojibake PDF.

Run locally:    uvicorn app:app --host 0.0.0.0 --port 8000
Deploy: Railway (Dockerfile) or any FastAPI host. Auto-deploy via GitHub Actions.
"""
import io
import os
import tempfile

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import PlainTextResponse, HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from recover_pdf import recover, ensure_ref_font
from make_corrected_pdf import correct_pdf

app = FastAPI(title="PDF Text Recovery", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# Ensure the reference Georgia font exists (downloads if missing).
REF = ensure_ref_font(os.environ.get("GEORGIA_FONT_PATH"))


@app.get("/", response_class=HTMLResponse)
def index():
    return """<html><body style="font-family:sans-serif;margin:40px">
    <h2>PDF Text Recovery</h2>
    <p>Upload a PDF whose body text extracts as mojibake (broken/absent ToUnicode on a
    subsetted CID font). The service reconstructs the real text from the embedded
    font's glyph outlines.</p>
    <form action="/recover" method="post" enctype="multipart/form-data">
      <input type="file" name="file" accept=".pdf" required>
      <button type="submit">Recover text</button>
    </form>
    <form action="/correct-pdf" method="post" enctype="multipart/form-data">
      <input type="file" name="file" accept=".pdf" required>
      <button type="submit">Download corrected PDF (same layout, fixed text layer)</button>
    </form>
    <p><a href="/docs">API docs</a></p>
    </body></html>"""


@app.post("/recover", response_class=PlainTextResponse)
def recover_text(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a .pdf file")
    data = file.file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        text, stats = recover(path, ref_font=REF, verbose=False)
    except Exception as e:
        raise HTTPException(500, f"Recovery failed: {e}")
    finally:
        os.unlink(path)
    return text


@app.post("/correct-pdf")
def correct_pdf_endpoint(file: UploadFile = File(...)):
    """Return a corrected PDF: identical rendering/layout, working text layer."""
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a .pdf file")
    data = file.file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(data)
        inpath = tmp.name
    outpath = tempfile.mktemp(suffix="_corrected.pdf")
    try:
        correct_pdf(inpath, outpath, ref_font=REF, verbose=False)
        with open(outpath, "rb") as f:
            payload = f.read()
    except Exception as e:
        raise HTTPException(500, f"PDF correction failed: {e}")
    finally:
        os.unlink(inpath)
        if os.path.exists(outpath):
            os.unlink(outpath)
    return StreamingResponse(io.BytesIO(payload),
                             media_type="application/pdf",
                             headers={"Content-Disposition": "attachment; filename=corrected.pdf"})


@app.get("/health")
def health():
    return {"status": "ok"}
