"""Assemble reconstructed text for all pages and compare to OCR ground truth."""
import os, sys, re, html
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pikepdf
from recover import ContentInterpreter, GIDResolver, REF_GEORGIA


def unescape_entities(text):
    """Convert HTML entities the author never unescaped (&gt;, &lt;, &amp;, &#39;...)
    into their real characters."""
    return html.unescape(text)


def read_content(pdf, page):
    c = page.get('/Contents')
    if isinstance(c, pikepdf.Array):
        return b''.join(x.read_bytes() for x in c)
    return c.read_bytes()


def reconstruct(pdf_path, page_numbers=None, verbose=False):
    pdf = pikepdf.open(pdf_path)
    res = GIDResolver(REF_GEORGIA, verbose=verbose)
    res.pdf = pdf
    pages = page_numbers if page_numbers is not None else range(len(pdf.pages))
    all_text = []
    for pno in pages:
        page = pdf.pages[pno]
        interp = ContentInterpreter(
            lambda fn, pg, r=res: r.resolve(fn, pg),
            widths_resolver=lambda fo, pg, r=res: r.widths_for_font(fo))
        data = read_content(pdf, page)
        interp._parse_stream(data, page)
        lines = group_lines(interp.runs)
        page_txt = '\n'.join(unescape_entities(l) for l in lines)
        all_text.append((pno, page_txt))
    return all_text


GAP_SPACE_THRESHOLD = 1.5  # min gap (points) before inserting a space between runs


def group_lines(runs, y_tol=4.0):
    """Group runs into lines by baseline y (PDF y is bottom-up), sort by x.

    Runs are concatenated, inserting a single space only when there is a real
    positional gap between the end of one run and the start of the next. Runs
    whose glyphs advance continuously (e.g. an HTML entity split across Arial /
    Georgia runs as '&','gt',';') stay glued, so entity unescaping works.
    Runs: (font, x_start, y, text, advance_points)
    """
    if not runs:
        return []
    lines = []
    for r in sorted(runs, key=lambda r: -r[2]):
        placed = False
        for line in lines:
            if abs(line['y'] - r[2]) <= y_tol:
                line['items'].append(r)
                placed = True
                break
        if not placed:
            lines.append({'y': r[2], 'items': [r]})
    out = []
    for line in lines:
        items = sorted(line['items'], key=lambda r: r[1])
        text = ''
        prev_end = None
        for r in items:
            font, x, y, t, adv = r
            if text and prev_end is not None:
                if (x - prev_end) > GAP_SPACE_THRESHOLD:
                    if not text.endswith(' '):
                        text += ' '
            text += t
            prev_end = x + (adv if adv else 0)
        text = re.sub(r' {2,}', ' ', text)
        out.append(text.strip())
    return out


if __name__ == '__main__':
    import json
    path = sys.argv[1] if len(sys.argv) > 1 else 'uploads/Anaesthesia_ed8.pdf'
    pages = [int(x) for x in sys.argv[2].split(',')] if len(sys.argv) > 2 else None
    results = reconstruct(path, pages, verbose=True)
    for pno, txt in results:
        print('=' * 80)
        print(f'PAGE {pno+1}')
        print('-' * 80)
        print(txt)
        print()
