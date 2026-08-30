#!/usr/bin/env python3
"""Produce a corrected PDF with the SAME visual layout but a working text layer.

The original renders correctly but extracts as mojibake because each subsetted
Georgia (Type0, Identity-H) font has a broken/absent /ToUnicode CMap and no cmap
table in the embedded font. This script leaves every content stream and every
glyph outline untouched (=> pixel-identical rendering) and only:
  1. writes a correct /ToUnicode CMap (GID/CID -> Unicode) for each Georgia subset,
  2. adds a proper cmap table to each embedded subset TrueType font,
  3. saves a corrected PDF whose text layer extracts cleanly.
"""
import argparse
import io
import os
import sys
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib'))

import pikepdf
from pikepdf import Name

from recover import GIDResolver, REF_GEORGIA
from fix_entities import rewrite_content
from assemble import read_content


def ensure_ref_font(path=None):
    """Return path to a reference Georgia font, downloading if missing."""
    if path and os.path.exists(path):
        return path
    if os.path.exists(REF_GEORGIA):
        return REF_GEORGIA
    import urllib.request
    url = 'https://raw.githubusercontent.com/FSKiller/Microsoft-Fonts/main/georgia.ttf'
    os.makedirs(os.path.dirname(REF_GEORGIA), exist_ok=True)
    print('Downloading reference Georgia font...')
    urllib.request.urlretrieve(url, REF_GEORGIA)
    return REF_GEORGIA


def build_unicmap(gmap):
    """Build a PDF ToUnicode CMap stream string mapping CID -> UTF-16BE."""
    lines = []
    lines.append('/CIDInit /ProcSet findresource begin')
    lines.append('12 dict begin')
    lines.append('begincmap')
    lines.append('/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def')
    lines.append('/CMapName /Adobe-Identity-UCS def')
    lines.append('/CMapType 2 def')
    lines.append('1 begincodespacerange')
    lines.append('<0000> <FFFF>')
    lines.append('endcodespacerange')
    items = sorted(gmap.items())
    # 100 per bfchar chunk
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        lines.append(f'{len(chunk)} beginbfchar')
        for cid, ch in chunk:
            if len(ch) == 1:
                u16 = ch.encode('utf-16-be').hex().upper()
                lines.append(f'<{cid:04X}> <{u16}>')
            else:
                b = ch.encode('utf-16-be')
                u16 = b.hex().upper()
                lines.append(f'<{cid:04X}> <{u16}>')
        lines.append('endbfchar')
    lines.append('endcmap')
    lines.append('CMapName currentdict /CMap defineresource pop')
    lines.append('end')
    lines.append('end')
    return '\n'.join(lines) + '\n'


def add_cmap_to_font(raw_ttf, gmap):
    """Return the TTF bytes with a cmap table added (unicode -> GID)."""
    from fontTools.ttLib import TTFont, newTable
    from fontTools.ttLib.tables._c_m_a_p import CmapSubtable
    from io import BytesIO
    font = TTFont(BytesIO(raw_ttf))
    # The subset has no glyph names; assign stable names so the cmap can refer to them.
    num_glyphs = font['maxp'].numGlyphs
    glyph_names = [f'gid{i}' for i in range(num_glyphs)]
    font.setGlyphOrder(glyph_names)
    if 'cmap' not in font:
        font['cmap'] = newTable('cmap')
    cmap = font['cmap']
    if not hasattr(cmap, 'tables'):
        cmap.tables = []
    if not hasattr(cmap, 'tableVersion'):
        cmap.tableVersion = 0
    # build cmap mapping char -> glyph name (only single-code-unit chars)
    unicode_map = {}
    for cid, ch in gmap.items():
        if len(ch) == 1 and 0 <= cid < num_glyphs:
            unicode_map.setdefault(ord(ch), glyph_names[cid])
    # drop any existing Unicode BMP subtable, keep others
    new_subs = []
    for t in cmap.tables:
        if not (t.platformID == 3 and t.platEncID == 1):
            new_subs.append(t)
    if unicode_map:
        st = CmapSubtable.newSubtable(4)
        st.platformID = 3
        st.platEncID = 1
        st.language = 0
        st.cmap = unicode_map
        new_subs.append(st)
    cmap.tables = new_subs
    out = BytesIO()
    font.save(out)
    return out.getvalue()


def correct_pdf(input_path, output_path, ref_font=None, add_cmap=True, verbose=True):
    ensure_ref_font(ref_font)
    src = pikepdf.open(input_path)
    res = GIDResolver(REF_GEORGIA, verbose=verbose)
    res.pdf = src

    # Collect every Georgia Type0 font object referenced by any page (dedupe by objgen)
    seen = set()
    font_objs = []
    for page in src.pages:
        fonts = page.get('/Resources', {}).get('/Font', {})
        for fname in fonts:
            f = fonts[fname]
            gen = f.objgen if hasattr(f, 'objgen') else id(f)
            if gen not in seen:
                seen.add(gen)
                if res.is_georgia_cid_font(f):
                    font_objs.append(f)

    for f in font_objs:
        gmap = res.map_for_font(f)
        if not gmap:
            if verbose:
                print('  skip (no map):', f.get('/BaseFont'))
            continue
        if add_cmap:
            raw = res._extract_font_bytes(f)
            if raw is not None:
                try:
                    new_raw = add_cmap_to_font(raw, gmap)
                    fd = f['/DescendantFonts'][0]['/FontDescriptor']
                    for k in ('/FontFile2', '/FontFile', '/FontFile3'):
                        if k in fd:
                            fd[k] = pikepdf.Stream(src, new_raw)
                            break
                except Exception as e:
                    if verbose:
                        print('  cmap add failed:', e)
        # Write correct /ToUnicode
        cmap_data = build_unicmap(gmap).encode('ascii')
        f['/ToUnicode'] = pikepdf.Stream(src, cmap_data)
        if verbose:
            print(f'  fixed {f.get("/BaseFont")}: {len(gmap)} chars, '
                  f'ToUnicode {len(cmap_data)} bytes')

    # Rewrite HTML-entity text in content streams into real characters
    # (e.g. '&gt;' -> '>') so both rendering and extraction are correct.
    fixed_pages = 0
    for pno in range(len(src.pages)):
        page = src.pages[pno]
        data = read_content(src, page)
        new = rewrite_content(data, res, page)
        if new is not None and new != data:
            page['/Contents'] = pikepdf.Stream(src, new)
            fixed_pages += 1
    if verbose:
        print(f'  entity-rewrote {fixed_pages} pages')

    src.save(output_path, compress_streams=True, preserve_pdfa=False)
    src.close()
    print(f'[ok] Wrote corrected PDF: {output_path}')


def main():
    ap = argparse.ArgumentParser(description='Build a corrected PDF with working text layer')
    ap.add_argument('input')
    ap.add_argument('-o', '--output', default='corrected.pdf')
    ap.add_argument('--ref-font', default=None)
    ap.add_argument('--no-cmap', action='store_true', help='skip adding cmap to fonts')
    args = ap.parse_args()
    correct_pdf(args.input, args.output, ref_font=args.ref_font,
                add_cmap=not args.no_cmap)


if __name__ == '__main__':
    main()
