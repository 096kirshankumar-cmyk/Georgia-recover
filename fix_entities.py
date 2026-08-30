"""Rewrite HTML-entity text in a PDF content stream into real characters.

The original document literally types `&gt;`, `&lt;`, `&amp;`, `&#39;` etc. as
plain text (often split across Arial `&`/`;` and Georgia `gt` runs). This
rewrites those glyph sequences into a single real character so BOTH the
rendered page and the extracted text show e.g. `>` instead of `&gt;`.

Approach: tokenize the content stream, reconstruct the logical character
sequence (with the token index that produced each char), find HTML entities,
and re-emit the stream with each entity's glyph tokens replaced by a single
literal `(char)Tj` in the font that is active at the entity start (Arial).
"""
import re
import html
import pikepdf
from pikepdf import Name
from recover import parse_cid_w


def tokenize(data):
    """Yield (kind, raw) with byte offsets. Kinds: num,name,op,str,hex,array."""
    items = []
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        if c in b' \t\r\n\x0c':
            i += 1; continue
        if c == 0x25:
            while i < n and data[i] not in b'\r\n':
                i += 1
            continue
        if c == 0x28:
            j = i + 1; depth = 1
            while j < n and depth:
                if data[j] == 0x5c: j += 2; continue
                if data[j] == 0x28: depth += 1
                elif data[j] == 0x29: depth -= 1
                j += 1
            items.append(('str', data[i:j], i, j)); i = j
        elif c == 0x3c:
            j = i + 1
            while j < n and data[j] != 0x3e:
                j += 1
            items.append(('hex', data[i:j + 1], i, j + 1)); i = j + 1
        elif c == 0x5b:
            j = i + 1; depth = 1
            while j < n and depth:
                if data[j] == 0x5b: depth += 1
                elif data[j] == 0x5d: depth -= 1
                j += 1
            items.append(('array', data[i:j], i, j)); i = j
        elif c == 0x2f:
            j = i + 1
            while j < n and data[j] not in b' \t\r\n\x0c()<>[]{}/%':
                j += 1
            items.append(('name', data[i:j], i, j)); i = j
        elif (0x30 <= c <= 0x39) or c in b'+-.':
            j = i
            if c in b'+-': j += 1
            while j < n and (0x30 <= data[j] <= 0x39 or data[j] in b'.-'):
                j += 1
            items.append(('num', data[i:j], i, j)); i = j
        else:
            j = i
            while j < n and data[j] not in b' \t\r\n\x0c()<>[]{}/%':
                j += 1
            items.append(('op', data[i:j], i, j)); i = j
    return items


def decode_literal_chars(body):
    """Decode a PDF literal-string body (no surrounding parens) to unicode chars."""
    out = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == 0x5c and i + 1 < n:
            nxt = body[i + 1]
            if nxt in b'nrtbf':
                out.append({ord('n'):10, ord('r'):13, ord('t'):9,
                            ord('b'):8, ord('f'):12}[nxt]); i += 2
            elif nxt in b'()\\':
                out.append(nxt); i += 2
            elif 0x30 <= nxt <= 0x37:
                j = i + 1; v = 0; cnt = 0
                while j < n and cnt < 3 and 0x30 <= body[j] <= 0x37:
                    v = v * 8 + (body[j] - 0x30); j += 1; cnt += 1
                out.append(v); i = j
            else:
                out.append(nxt); i += 2
        else:
            out.append(c); i += 1
    try:
        return list(bytes(out).decode('cp1252'))
    except Exception:
        return list(bytes(out).decode('latin-1', errors='replace'))


def literal_body(ch):
    """Return PDF-literal body bytes for a character, else None if unencodable.

    Encodes via WinAnsi (cp1252) so bullets etc. are preserved, and escapes the
    PDF-special bytes `(` `)` `\\`."""
    try:
        b = ch.encode('cp1252')
    except UnicodeEncodeError:
        return None
    out = bytearray()
    for byte in b:
        if byte in (0x28, 0x29, 0x5C):  # ( ) \
            out.append(0x5C)
        out.append(byte)
    return bytes(out)


def build_units(items, res, page):
    """Walk ops, produce list of (char, token_index, font_name)."""
    units = []
    cur_font = None
    stack = []
    idx = 0
    n = len(items)

    def get_char_units_from_bytes(body, tokidx):
        return [(c, tokidx, cur_font) for c in decode_literal_chars(body)]

    def cid_units(hexstr, tokidx):
        if cur_font is None:
            return []
        gmap = res.resolve(cur_font, page)
        if not gmap:
            return []
        h = hexstr[1:-1].decode('latin-1') if hexstr.startswith(b'<') else hexstr.decode('latin-1')
        out = []
        for k in range(0, len(h) - 1, 2):
            cid = int(h[k:k + 2], 16)
            ch = gmap.get(cid, '')
            if ch:
                out.append((ch, tokidx, cur_font))
        return out

    while idx < n:
        kind, raw, _, _ = items[idx]
        if kind == 'op':
            op = raw.decode('latin-1')
            if op == 'Tf':
                stack and stack.pop()  # size
                f = stack.pop() if stack else None
                if f and f[0] == 'name':
                    raw = f[1]
                    name = raw.decode('latin-1') if isinstance(raw, bytes) else str(raw)
                    cur_font = name  # keep leading '/' to match pikepdf.Name keys
            elif op in ('Tj', "'"):
                o = stack.pop() if stack else None
                if o and o[0] == 'str':
                    units.extend(get_char_units_from_bytes(o[1][1:-1], o[2]))
                elif o and o[0] == 'hex':
                    units.extend(cid_units(o[1], o[2]))
            elif op == '"':
                o = stack.pop() if stack else None
                if o and o[0] == 'str':
                    units.extend(get_char_units_from_bytes(o[1][1:-1], o[2]))
            elif op == 'TJ':
                o = stack.pop() if stack else None
                if o and o[0] == 'array':
                    arr = o[1]
                    for m in re.finditer(rb'<([0-9A-Fa-f]+)>', arr):
                        units.extend(cid_units(m.group(1), o[2]))
            # ignore other ops
        else:
            stack.append((kind, raw, idx))
        idx += 1
    return units


class _Widths:
    """Resolve per-glyph advance (in 1000-unit font space) for a page's fonts."""

    def __init__(self, res, page):
        self.res = res
        self.page = page
        self._simple = {}
        self._cid = {}
        try:
            fonts = page['/Resources']['/Font']
            for name in fonts:
                f = fonts[name]
                nm = '/' + str(name).lstrip('/') if not str(name).startswith('/') else str(name)
                try:
                    sub = f.get('/Subtype', None)
                    if sub == Name('/Type0'):
                        df = f['/DescendantFonts'][0]
                        w = parse_cid_w(list(df['/W']))
                        gmap = res.map_for_font(f)
                        rev = {}
                        for cid, ch in gmap.items():
                            rev.setdefault(ch, cid)
                        self._cid[nm] = (w, rev)
                    elif f.get('/Widths') is not None:
                        first = int(f.get('/FirstChar', 0))
                        widths = [int(x) for x in f['/Widths']]
                        self._simple[nm] = (first, widths)
                except Exception:
                    pass
        except Exception:
            pass

    def width(self, font, char):
        if font in self._cid:
            w, rev = self._cid[font]
            cid = rev.get(char)
            if cid is not None:
                return w.get(cid, 500)
            return 500
        if font in self._simple:
            first, widths = self._simple[font]
            try:
                code = char.encode('cp1252')[0]
            except Exception:
                return 500
            i = code - first
            if 0 <= i < len(widths):
                return widths[i]
            return 500
        return 500


ENTITY_RE = re.compile(
    r'&(?:gt|lt|amp|quot|apos|nbsp|copy|reg|trade|ndash|mdash|hellip|euro|times|divide|le|ge|middot|plusmn|deg|\#\d+|\#x[0-9A-Fa-f]+);')


def find_entity_spans(units):
    """Return list of (start_unit_idx, end_unit_idx, char)."""
    chars = ''.join(c for c, *_ in units)
    spans = []
    for m in ENTITY_RE.finditer(chars):
        s, e = m.span()
        # units are 1 char each -> unit idx == char offset
        u0, u1 = s, e
        if u1 > len(units):
            continue
        if ''.join(c for c, *_ in units[u0:u1]) != m.group(0):
            continue
        char = html.unescape(m.group(0))
        spans.append((u0, u1, char))
    return spans


def rewrite_content(data, res, page):
    """Return new content bytes with entities replaced, or None if no change.

    Replaces each entity's glyphs with the real character, PRESERVING any
    leading/trailing non-entity characters that share the same tokens (e.g. the
    leading space in `( &)`) so no visible gap is introduced. It also consumes
    the trailing text-show operator after the last entity literal so we never
    emit a doubled `Tj`.
    """
    items = tokenize(data)
    units = build_units(items, res, page)
    if not units:
        return None
    spans = find_entity_spans(units)
    if not spans:
        return None
    widths = _Widths(res, page)

    # Build per-entity token ranges
    ent_tokens = []  # (first_token, last_token, [unit_indices], [replace_chars])
    for u0, u1, char in spans:
        toks = sorted({units[k][1] for k in range(u0, u1)})
        if not toks:
            continue
        if literal_body(char) is None:
            continue
        ent_tokens.append((toks[0], toks[-1], list(range(u0, u1)), char))

    if not ent_tokens:
        return None

    # Merge entities whose token ranges overlap (e.g. adjacent &gt;&gt; share a
    # literal like '(;&)'). Overlapping byte ranges would otherwise corrupt the
    # stream, so we replace each merged group once.
    ent_tokens.sort(key=lambda e: e[0])
    groups = []
    for ft, lt, uis, ch in ent_tokens:
        if groups and ft <= groups[-1][1]:  # overlaps previous group
            gft, glt, g_uis, g_chs = groups[-1]
            groups[-1] = (gft, max(glt, lt), sorted(set(g_uis + uis)), g_chs + ch)
        else:
            groups.append((ft, lt, uis, ch))

    replacements = []  # (start_byte, end_byte, replacement_bytes)
    for first, last, uis, repl_chars in groups:
        # all entity unit indices in this group
        entity_set = set(uis)
        # chars in the covered token range that are NOT part of the entities
        preserve = []
        for u in range(len(units)):
            t = units[u][1]
            if first <= t <= last and u not in entity_set:
                preserve.append((units[u][0], units[u][2]))
        # build literal body (preserved + replacement chars)
        repl_lit = ''.join(p[0] for p in preserve) + repl_chars
        body = b''
        ok = True
        for ch in repl_lit:
            b = literal_body(ch)
            if b is None:
                ok = False
                break
            body += b
        if not ok:
            continue
        # advance compensation: original glyphs vs new glyphs
        repl_font = units[uis[0]][2]
        orig_adv = sum(widths.width(units[u][2], units[u][0]) for u in uis) \
            + sum(widths.width(f, c) for c, f in preserve)
        new_adv = sum(widths.width(repl_font, c) for c, _ in preserve) \
            + sum(widths.width(repl_font, ch) for ch in repl_chars)
        delta_1000 = int(round(orig_adv - new_adv))
        start = items[first][2]
        end = items[last][3]
        nxt = last + 1
        if nxt < len(items) and items[nxt][0] == 'op' and items[nxt][1] in (b'Tj', b'TJ', b"'", b'"'):
            end = items[nxt][3]
        if delta_1000:
            repl = b'[(' + body + b') ' + str(-delta_1000).encode() + b'] TJ '
        else:
            repl = b'(' + body + b')Tj '
        replacements.append((start, end, repl))

    if not replacements:
        return None
    replacements.sort(key=lambda r: r[0])
    # drop any still-overlapping (shouldn't happen after merging, but be safe)
    merged_repl = []
    for r in replacements:
        if merged_repl and r[0] < merged_repl[-1][1]:
            continue  # skip overlapping (defensive)
        merged_repl.append(r)
    out = bytearray(data)
    for start, end, repl in reversed(merged_repl):
        out[start:end] = repl
    return bytes(out)
