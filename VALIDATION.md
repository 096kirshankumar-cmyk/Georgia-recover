# Validation report

Tested in a clean sandbox (Python 3.13) against `Anaesthesia_ed8.pdf` (418 pages,
~6.8 MB) — the same family of "garbled-body" files as `OBGYN_ed8_CLEAN.pdf`.

## Root cause confirmed
- Body font: **subsetted Georgia, Type0, `/Identity-H`**, `CIDToGIDMap = Identity`
  ⇒ CID == GID.
- `/ToUnicode` CMap exists but is **broken**: every CID maps to `U+FFFD`, then a
  bogus CP1252 block for `0x20+`.
- Embedded `/FontFile2` has **no `cmap`, no `post` (glyph-name)** tables — only
  outlines (`glyf`) + `name`. → hardest case; recovered by **glyph-outline matching**.

## Glyph-outline matching accuracy
On a hand-verified sample (21 glyphs of the `LSRITB` subset), matching each subset
GID against the reference Georgia font by **ink-bitmap IoU**:

- 21 / 21 correct
- **IoU = 1.000** for every glyph (subset outlines are identical to the source font)

## Full-document run
- 418 pages recovered in **~13 s** (CLI).
- **25 distinct Georgia subsets** mapped automatically.
- **0 unmapped CIDs** across the whole document.

## "Matches the rendering visual layer"
Extracted text was compared with **OCR of the rasterised page** (FreeType render +
Tesseract). Ground-truth line on page 5:

> "The prototype of the device shown in the image below was originally named
> after which personality ?"

Recovered text contains exactly that sentence. Cross-checked on pages 5, 120, 250
(spans 3 different chapters/subsets): all words present and correct.

## API test
- `GET /health` → `{"status":"ok"}`
- `POST /recover` with the 418-page PDF → **HTTP 200**, full recovered text in
  ~13 s.
- `POST /correct-pdf` with the 418-page PDF → **HTTP 200**, returns a corrected
  PDF (~7 MB) in ~6 s.

## Corrected PDF mode (same layout, fixed text layer)
`make_corrected_pdf.py` leaves every content stream and every glyph outline
untouched (rendering stays **pixel-identical**) and only:
1. writes a correct `/ToUnicode` CMap (GID/CID → Unicode) for each Georgia subset,
2. adds a proper `cmap` table to each embedded subset font.

Verification:
- **Rendering identical:** original vs corrected rasterised at 100 dpi on pages
  1, 5, 6, 7, 120, 250, 301, 418 → **total pixel diff = 0** (pixel-identical).
- **Text layer correct:** PyMuPDF AND pdfminer.six both extract clean, correct
  text from the corrected PDF (page 5 → "The prototype of the device ...").
- **418 pages, 25 subsets fixed** in ~2 s.

The corrected PDF (`Anaesthesia_ed8_CORRECTED.pdf`) is the primary deliverable —
same layout as the original, but copy/search/extract now returns the real text.

## HTML-entity symbols (e.g. `&gt;` → `>`)
The original generator left HTML entities as literal text (split across Arial
`&`/`;` and Georgia `gt` runs). `fix_entities.py` rewrites each entity's glyph
run into a single real character in the content stream, so BOTH the rendered
page and the extracted text show `>` instead of `&gt;`.

Verification:
- 54 pages were entity-rewritten.
- **0 leftover entities** (`&gt;`/`&lt;`/`&amp;`/`&#39;`...) anywhere in the
  corrected PDF (full 418-page scan).
- Non-entity pages remain **pixel-identical** to the original (diff = 0).
- Entity pages now render and extract the real symbol, e.g. page 101 option
  `a) D>E>F>A` (was `D&gt;E&gt;F&gt;A`).
- All 418 pages render with **0 errors** after rewriting.

## Sample recovered output (page 7)
```
Question 10: Passive euthanasia was recently made legal in which of the following countries ?
a) b) c) India Belgium Netherlands
d) Luxembourg
Answer Key
Question No. Correct Option
1 2 3 b c c
4 5 6 a a b
7 8 9 a d b
10 a
Detailed Explanations
Solution to Question 1:
October 16th is celebrated every year as " World Anesthesia Day " the first successful
demonstration of diethyl ether anesthesia to perform painless surgeries . or " Ether day "
to commemorate
```
