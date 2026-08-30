# PDF Text Recovery

Recover readable text from PDFs whose body text extracts as **mojibake** — even
though the page *renders* correctly.

## The problem this solves

Some question-bank PDFs (e.g. `Anaesthesia_ed8.pdf`, `OBGYN_ed8_CLEAN.pdf`) ship
with a subsetted, embedded **Georgia Type0 font** whose `/ToUnicode` CMap is
**missing or broken** (it maps CIDs to `U+FFFD`). The result:

| layer | behaviour |
|-------|-----------|
| Rendering | correct — rendering only needs `char code → glyph outline` |
| Extraction | mojibake — extraction needs `char code → Unicode` |

No OCR-language setting, no `cp1252`, and no high-level extractor fixes it. The
fix is **low-level mapping reconstruction**.

## The recovery method

```
raw content-stream bytes  →  character code / CID  →  GID  →  glyph → Unicode
```

1. Parse the content stream with `pikepdf`.
2. Pull the raw **2-byte CID** runs from `Tj` / `TJ` (font is `Identity-H`,
   `CIDToGIDMap = Identity`, so **CID == GID**).
3. Extract the embedded subset font (`/FontFile2`).
4. The subset has **no `cmap` / `post` (glyph-name)** tables → use
   **glyph-outline matching**:
   - render each GID with **FreeType** (`FT_LOAD_RENDER | FT_LOAD_NO_HINTING`),
   - crop to the ink bbox, normalise to a fixed grid,
   - match against a reference **Georgia** font's glyphs by **IoU**.
     For a true subset the outline is identical, so `IoU == 1.0`.
5. Decode every CID run through that per-subset `GID → char` map.
6. Word spaces are encoded as large negative kerning adjustments in the `TJ`
   arrays; a threshold on those gaps re-inserts spaces.
7. Reconstruct lines from the text-matrix (`Tm`) baseline positions.

Verified in-repo: extracted text **matches the rendered layer** (OCR of the
rasterised page) with **100 %** glyph accuracy on a known sample.

## CLI

```bash
pip install -r requirements.txt

# Recover one page to stdout
python recover_pdf.py input.pdf -p 5

# Recover the whole document to a file
python recover_pdf.py input.pdf -o recovered.txt

# ALSO build a corrected PDF: identical layout, fixed (searchable/copyable) text layer
python recover_pdf.py input.pdf -f corrected.pdf

# Or build just the corrected PDF
python make_corrected_pdf.py input.pdf -o corrected.pdf
```

The reference Georgia font is downloaded automatically on first run
(see `GEORGIA_FONT_PATH` env var to point at your own copy).

## Web service (FastAPI)

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
# POST a PDF to /recover           -> returns recovered plain text
# POST a PDF to /correct-pdf       -> downloads a corrected PDF
```

## Deploy to Railway

Either point Railway at the repo root (it uses the `Dockerfile`), or:

1. Push this repo to GitHub.
2. In Railway create a new project and link the repo (Dockerfile is detected
   automatically via `railway.json`).
3. Add a `RAILWAY_TOKEN` to GitHub Secrets to enable **auto-deploy** with
   `.github/workflows/deploy.yml` (add a `RAILWAY_SERVICE` variable if your
   service name differs).

### Required env vars
- `PORT` — set by Railway automatically.
- `GEORGIA_FONT_PATH` *(optional)* — path to a reference `Georgia.TTF`. If unset,
  the service downloads one at startup.

## Corrected PDF mode

The best deliverable is a **corrected PDF**:

1. each Georgia subset gets a correct `/ToUnicode` CMap (GID/CID → Unicode), and
2. each embedded subset font gets a proper `cmap` table, and
3. literal HTML entities (`&gt;`→`>`, `&lt;`→`<`, `&amp;`→`&`, `&#39;`→`'`, ...)
   are rewritten into real characters so the symbol is shown and extracted
   correctly instead of as its raw entity text.

Glyph outlines are otherwise untouched, so pages **render pixel-identically**
except on the (already-broken) entity lines. Result: copy/search/extract returns
clean, readable text with the original layout.

> Note: `make_corrected_pdf.py` uses the same reference Georgia font as the
> recovery engine to build each font's Unicode map.

## Project layout
```
.
├── app.py               # FastAPI service
├── recover_pdf.py       # CLI + recovery entry point (+ -f to build corrected PDF)
├── make_corrected_pdf.py# build a corrected PDF with a working text layer
├── fix_entities.py      # rewrite HTML-entity text (&gt; -> >) in content streams
├── lib/
│   ├── recover.py       # content-stream interpreter + GID resolver
│   ├── glyphmatch.py    # FreeType glyph-outline matcher (IoU)
│   └── assemble.py      # line reconstruction
├── Dockerfile
├── Procfile
├── railway.json
└── .github/workflows/deploy.yml
```

## License / font note
The reference **Georgia** font is a Microsoft core font distributed under its
own EULA. It is *not* committed to this repo; it is fetched at runtime, so the
recovery tool itself remains redistributable.
