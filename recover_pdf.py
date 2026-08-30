#!/usr/bin/env python3
"""Recover readable text from the 'garbled' OBGYN/Anaesthesia-style PDF.

Problem
-------
The body font is a subsetted embedded Georgia (Type0, Identity-H). Its
`/ToUnicode` CMap is missing/broken (maps CIDs to U+FFFD), so ordinary text
extractors emit mojibake. Rendering works because rendering only needs
character-code -> glyph-outline, not -> Unicode.

Recovery (low-level)
--------------------
1. Parse each page's content stream with pikepdf.
2. Pull the raw 2-byte CID runs from Tj/TJ operators (CID == GID, Identity).
3. Extract the embedded subset font (FontFile2) and parse it.
4. Because the subset has NO cmap/post (glyph-name) tables, build the
   GID -> Unicode map by *glyph-outline matching*: render each GID with
   FreeType (no hinting) and match its ink bitmap against a reference
   Georgia font's glyphs using IoU. (IoU == 1.0 for real subsets.)
5. Decode every CID run through that per-subset map.
6. Reconstruct lines from the text-matrix positions.
"""
import argparse
import os
import sys
import hashlib
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib'))

import pikepdf
from recover import ContentInterpreter, GIDResolver, REF_GEORGIA
from assemble import read_content, group_lines, unescape_entities


def ensure_ref_font(path=None):
    """Return path to a reference Georgia font, downloading if necessary."""
    if path and os.path.exists(path):
        return path
    if os.path.exists(REF_GEORGIA):
        return REF_GEORGIA
    # download fallback
    import urllib.request
    url = 'https://raw.githubusercontent.com/FSKiller/Microsoft-Fonts/main/georgia.ttf'
    dst = REF_GEORGIA
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    print('Downloading reference Georgia font...')
    urllib.request.urlretrieve(url, dst)
    return dst


def recover(input_pdf, output_txt=None, page_numbers=None, ref_font=None, verbose=True):
    ref_font = ensure_ref_font(ref_font)
    pdf = pikepdf.open(input_pdf)
    res = GIDResolver(ref_font, verbose=verbose)
    res.pdf = pdf
    pages = page_numbers if page_numbers is not None else range(len(pdf.pages))
    out_lines = []
    stats = {'pages': 0, 'unmapped_cids': 0, 'subsets': len(res.map_cache)}
    for pno in pages:
        page = pdf.pages[pno]
        interp = ContentInterpreter(
            lambda fn, pg, r=res: r.resolve(fn, pg),
            widths_resolver=lambda fo, pg, r=res: r.widths_for_font(fo))
        data = read_content(pdf, page)
        interp._parse_stream(data, page)
        lines = [unescape_entities(l) for l in group_lines(interp.runs)]
        out_lines.append(f'===== PAGE {pno+1} =====')
        out_lines.extend(lines)
        out_lines.append('')
        stats['pages'] += 1
    text = '\n'.join(out_lines)
    if output_txt:
        with open(output_txt, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f'Wrote {output_txt} ({len(text)} chars)')
    stats['subsets'] = len(res.map_cache)
    return text, stats


def main():
    ap = argparse.ArgumentParser(description='Recover readable text from garbled PDF')
    ap.add_argument('input')
    ap.add_argument('-o', '--output', default=None,
                    help='write recovered plain text to this file')
    ap.add_argument('-p', '--pages', default=None,
                    help='comma-separated 1-based page numbers to process')
    ap.add_argument('--ref-font', default=None, help='path to reference Georgia TTF')
    ap.add_argument('-f', '--fix-pdf', metavar='OUT.pdf', default=None,
                    help='also build a corrected PDF (same layout, fixed text layer)')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()
    pages = [int(x) - 1 for x in args.pages.split(',')] if args.pages else None
    text, stats = recover(args.input, args.output, page_numbers=pages,
                          ref_font=args.ref_font, verbose=not args.quiet)
    if not args.output:
        sys.stdout.write(text)
    if args.fix_pdf:
        from make_corrected_pdf import correct_pdf
        correct_pdf(args.input, args.fix_pdf, ref_font=args.ref_font,
                    add_cmap=True, verbose=not args.quiet)
    print('\n[stats]', stats, file=sys.stderr)


if __name__ == '__main__':
    main()
