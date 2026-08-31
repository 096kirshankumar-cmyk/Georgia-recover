"""Recovery engine for the garbled-body PDF.

Reconstructs clean text by:
  * Parsing each page's content stream (PDF text operators).
  * Decoding Georgia Type0 (Identity-H) CID runs via glyph-outline matching
    against a reference Georgia font (per-subset GID->Unicode map).
  * Decoding literal WinAnsi strings for the Arial/simple fonts.
  * Rebuilding lines from the text matrix positions.
"""
import hashlib
import io
import os
import re
import struct
import pikepdf
from pikepdf import Name

from glyphmatch import load_face, ReferenceLibrary

WINANSI_PATTERN = re.compile(rb'\((?:[^()\\]|\\.)*\)')
HEX_PATTERN = re.compile(r'<([0-9A-Fa-f]+)>')


def parse_cid_w(arr):
    """Parse a PDF /W array into {cid: width_in_1000}."""
    widths = {}
    i = 0
    n = len(arr)
    while i < n:
        first = int(arr[i])
        if i + 1 < n and isinstance(arr[i + 1], (list, tuple, pikepdf.Array)):
            # explicit width list
            wlist = arr[i + 1]
            for j, w in enumerate(wlist):
                widths[first + j] = int(w)
            i += 2
        else:
            # c_first c_last w  (constant width range)
            if i + 2 < n:
                last = int(arr[i + 1])
                w = int(arr[i + 2])
                for c in range(first, last + 1):
                    widths[c] = w
                i += 3
            else:
                i += 1
    return widths

# Reference Georgia font path (cached download)
REF_GEORGIA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'ref_font', 'Georgia.TTF')


def decode_literal_bytes(b):
    """Decode a PDF literal-string body to its raw character-code bytes.

    Resolves newline/carriage/tab/backspace/formfeed, escaped parens and
    backslash, and up-to-3-digit octal escapes, returning the bytearray of
    character codes (before any font encoding).
    """
    out = bytearray()
    i = 0
    while i < len(b):
        c = b[i]
        if c == 0x5c and i + 1 < len(b):  # backslash escape
            n = b[i + 1]
            if n in b'nrtbf':
                out.append({ord('n'):10, ord('r'):13, ord('t'):9,
                            ord('b'):8, ord('f'):12}[n])
                i += 2
            elif n == 0x28 or n == 0x29 or n == 0x5c:
                out.append(n); i += 2
            elif 0x30 <= n <= 0x37:
                j = i + 1; v = 0; cnt = 0
                while j < len(b) and cnt < 3 and 0x30 <= b[j] <= 0x37:
                    v = v * 8 + (b[j] - 0x30); j += 1; cnt += 1
                out.append(v); i = j
            else:
                out.append(n); i += 2
        else:
            out.append(c); i += 1
    return bytes(out)


def decode_literal(b):
    """Decode a PDF literal string (...) for WinAnsiEncoded simple fonts."""
    # strip surrounding parens handled by caller; here b is the raw paren-body bytes
    out = bytearray()
    i = 0
    while i < len(b):
        c = b[i]
        if c == 0x5c and i + 1 < len(b):  # backslash escape
            n = b[i + 1]
            if n in b'nrtbf':
                out.append({ord('n'):10, ord('r'):13, ord('t'):9,
                            ord('b'):8, ord('f'):12}[n])
                i += 2
            elif n == 0x28 or n == 0x29 or n == 0x5c:
                out.append(n); i += 2
            elif 0x30 <= n <= 0x37 and i + 3 < len(b):
                # octal up to 3 digits
                j = i + 1; v = 0; cnt = 0
                while j < len(b) and cnt < 3 and 0x30 <= b[j] <= 0x37:
                    v = v * 8 + (b[j] - 0x30); j += 1; cnt += 1
                out.append(v); i = j
            else:
                out.append(n); i += 2
        else:
            out.append(c); i += 1
    # WinAnsi is essentially cp1252 for the used bytes
    try:
        return bytes(out).decode('cp1252')
    except Exception:
        return bytes(out).decode('latin-1', errors='replace')


class ContentInterpreter:
    """Tiny PDF content-stream text interpreter."""

    def __init__(self, gid_map_resolver, widths_resolver=None):
        # gid_map_resolver(font_resource_name, page) -> gid->char dict or None
        self.resolve = gid_map_resolver
        self.widths_resolver = widths_resolver  # (font,page)->{code:width1000} or None
        self.font = None
        self.runs = []   # (font, x, y, text, advance_1000)
        self.font_size = 10
        # text matrix state
        self.tm = None
        self.in_text = False
        self._widths = {}
        self._wcache = {}

    def _parse_stream(self, data, page):
        # tokenizer
        i = 0
        n = len(data)
        tokens = []
        while i < n:
            c = data[i]
            if c in b' \t\r\n\x0c':
                i += 1; continue
            if c == 0x25:  # comment
                while i < n and data[i] not in b'\r\n':
                    i += 1
                continue
            if c == 0x28:  # literal string
                j = i + 1; depth = 1
                while j < n and depth:
                    if data[j] == 0x5c:
                        j += 2; continue
                    if data[j] == 0x28: depth += 1
                    elif data[j] == 0x29: depth -= 1
                    j += 1
                tokens.append(('str', data[i + 1:j - 1]))
                i = j
            elif c == 0x3c:  # hex string
                j = i + 1
                while j < n and data[j] != 0x3e:
                    j += 1
                tokens.append(('hex', data[i + 1:j]))
                i = j + 1
            elif c == 0x5b:  # array
                j = i + 1; depth = 1
                while j < n and depth:
                    if data[j] == 0x5b: depth += 1
                    elif data[j] == 0x5d: depth -= 1
                    j += 1
                tokens.append(('array', data[i + 1:j - 1]))
                i = j
            elif (c == 0x2f):  # name /Name
                j = i + 1
                while j < n and data[j] not in b' \t\r\n\x0c()<>[]{}/%':
                    j += 1
                tokens.append(('name', data[i:j]))
                i = j
            elif 0x30 <= c <= 0x39 or c in b'+-.':
                j = i
                if c in b'+-': j += 1
                while j < n and (0x30 <= data[j] <= 0x39 or data[j] in b'.-'):
                    j += 1
                # negative exponent, etc.
                tokens.append(('num', float(data[i:j]) if data[i:j] not in (b'.',) else 0.0))
                i = j
            else:
                # operator word
                j = i
                while j < n and data[j] not in b' \t\r\n\x0c()<>[]{}/%':
                    j += 1
                op = data[i:j]
                tokens.append(('op', op))
                i = j
        self._exec(tokens, page)

    def _exec(self, tokens, page):
        self._page = page
        stack = []
        for kind, val in tokens:
            if kind == 'op':
                op = val.decode('latin-1')
                self._do_op(op, stack, page)
            else:
                stack.append((kind, val))
        # flush pending array pieces handled in _do_op

    def _do_op(self, op, stack, page):
        def pop():
            return stack.pop() if stack else (None, None)

        if op == 'BT':
            self.in_text = True
            self.tm = [1,0,0,1,0,0]
        elif op == 'ET':
            self.in_text = False
        elif op == 'Tf':
            k2, size = pop()          # top = size
            kind, fname = pop()       # /Name
            self.font = fname.decode('latin-1') if fname else None
            self.font_size = size if isinstance(size, (int, float)) else 10
            if self.widths_resolver and self.font:
                if self.font not in self._wcache:
                    try:
                        fonts = self._page['/Resources']['/Font']
                        fo = fonts.get(pikepdf.Name(self.font))
                        self._wcache[self.font] = self.widths_resolver(fo, self._page) or {}
                    except Exception:
                        self._wcache[self.font] = {}
                self._widths = self._wcache[self.font]
            else:
                self._widths = {}
        elif op == 'Tm':
            f = pop()[1]; e = pop()[1]; d = pop()[1]; c = pop()[1]
            b = pop()[1]; a = pop()[1]
            self.tm = [a, b, c, d, e, f]
        elif op in ('Td', 'TD'):
            ty = pop()[1]; tx = pop()[1]
            if self.tm:
                self.tm[4] += tx
                self.tm[5] += ty
        elif op == 'T*':
            if self.tm:
                self.tm[5] -= self.leading if hasattr(self, 'leading') else self.font_size
        elif op == 'TL':
            k, v = pop()
            if isinstance(v, (int, float)):
                self.leading = v
        elif op in ('Tj', "'"):
            k, s = pop()
            if k == 'str':
                text = self._decode_bytes(s)
                adv = self._bytes_advance(s)
                self._advance(s, hexmode=False)
                self._emit(text, stack, page, adv)
            elif k == 'hex':
                self._emit_hex(s, stack, page)
        elif op == '"':
            # aw ac string
            k, s = pop()
            if k == 'str':
                text = self._decode_bytes(s)
                adv = self._bytes_advance(s)
                self._advance(s, hexmode=False)
                self._emit(text, stack, page, adv)
        elif op == 'TJ':
            k, arr = pop()
            if k == 'array':
                self._emit_tj(arr, stack, page)
        elif op == 'q':
            pass
        elif op == 'Q':
            pass
        else:
            # operand stack pushes are handled because non-op tokens already pushed
            pass

    def _current_pos(self):
        if not self.tm:
            return 0.0, 0.0
        return self.tm[4], self.tm[5]

    def _decode_bytes(self, literal):
        # Simple TrueType Georgia subsets (PyPDF2 style): char codes shown via
        # octal-escaped literal strings. Decode each byte through the font's
        # code->char map (cmap -> glyph-outline match). Other simple fonts use
        # WinAnsi.
        if self.font is not None and self._is_truetype_georgia():
            gmap = self.resolve(self.font, self._page)
            if gmap:
                # decode PDF escapes to char-code bytes, then map via cmap+glyph
                return ''.join(gmap.get(b, '') for b in decode_literal_bytes(literal))
        return decode_literal(literal)

    def _is_truetype_georgia(self):
        try:
            fo = self._page['/Resources']['/Font'].get(pikepdf.Name(self.font))
            if fo is None:
                return False
            return (str(fo.get('/Subtype', '')) == '/TrueType'
                    and 'Georgia' in str(fo.get('/BaseFont', '')))
        except Exception:
            return False

    def _advance(self, bytes_seq, hexmode=False):
        """Advance the text position by the shown glyphs' advance widths."""
        if not self._widths or not self.tm:
            return
        tot = 0.0
        if hexmode:
            h = bytes_seq.decode('latin-1')
            for k in range(0, len(h) - 1, 2):
                cid = int(h[k:k + 2], 16)
                tot += self._widths.get(cid, 0)
        else:
            for b in bytes_seq:
                tot += self._widths.get(b, 0)
        dev = tot * self.font_size / 1000.0
        self.tm[4] += dev
        self._pending_advance = dev

    def _emit(self, text, stack, page, advance1000=0.0):
        x, y = self._current_pos()
        if text:
            self.runs.append((self.font, x, y, text, advance1000))

    def _emit_hex(self, hexstr, stack, page):
        text = self._decode_cid(hexstr, stack, page)
        adv = self._hex_advance(hexstr)
        self._advance(hexstr, hexmode=True)
        self._emit(text, stack, page, adv)

    def _emit_tj(self, arr, stack, page):
        # parse array content: sequence of hex strings and numbers
        parts = re.findall(rb'<([0-9A-Fa-f]+)>|(-?\d+(?:\.\d+)?)', arr)
        buf = []
        adv = 0.0
        for m in parts:
            if m[0]:
                text = self._decode_cid(m[0], stack, page)
                buf.append(text)
                adv += self._hex_advance(m[0])
                # advance position by glyph widths
                self._advance(m[0], hexmode=True)
            elif m[1]:
                val = float(m[1])
                # TJ array numbers are in 1/1000 text units: shift position
                if self.tm:
                    self.tm[4] += val * self.font_size / 1000.0
                # negative large gap => word space
                if val < -120:
                    buf.append(' ')
        self._emit(''.join(buf), stack, page, adv)

    def _bytes_advance(self, literal):
        if not self._widths:
            return 0.0
        tot = sum(self._widths.get(b, 0) for b in literal)
        return tot * self.font_size / 1000.0

    def _hex_advance(self, hexstr):
        if not self._widths:
            return 0.0
        h = hexstr.decode('latin-1')
        tot = 0
        for k in range(0, len(h) - 1, 2):
            tot += self._widths.get(int(h[k:k + 2], 16), 0)
        return tot * self.font_size / 1000.0

    def _decode_cid(self, hexstr, stack, page):
        # 2-byte CIDs for Identity-H Georgia fonts
        if self.font is None:
            return ''
        gmap = self.resolve(self.font, page)
        if gmap is None:
            return ''
        chars = []
        h = hexstr.decode('latin-1')
        for k in range(0, len(h) - 1, 2):
            cid = int(h[k:k + 2], 16)
            ch = gmap.get(cid, '')
            if ch:
                chars.append(ch)
        return ''.join(chars)


class GIDResolver:
    """Builds gid->char maps per Georgia subset font via glyph matching."""

    def __init__(self, ref_font=REF_GEORGIA, verbose=True):
        self.ref_font = ref_font
        self.verbose = verbose
        self.map_cache = {}
        self._code_cache = {}
        self.pdf = None
        chars = self._default_chars()
        self.lib = ReferenceLibrary(ref_font, chars=chars)
        # face handles per subset keyed by embedded-font content hash
        self._faces = {}

    def _default_chars(self):
        import string
        s = (string.ascii_letters + string.digits
             + ' .,;:()[]-!?/\"\'%&*+#$@^_=<>|\\{}~'
             + '\u2019\u2018\u201c\u201d\u2013\u2014\u2026\u00a0\u00e9\u00e8\u00ee'
             + '\u00e2\u00ea\u00f4\u00fb\u00f6\u00fc\u00e4\u00eb\u00ef\u00df\u00e1\u00e0'
             + '\u010d\u0161\u017e\u00e7\u0153\u0152\u00f1\u00d1\u00f3\u00f2\u00fa\u00f9')
        return [ord(c) for c in s]

    @staticmethod
    def _extract_font_bytes(font):
        # Handles both Type0/CID fonts (FontFile2 under DescendantFonts'
        # FontDescriptor) and simple TrueType fonts (FontFile2 directly under
        # the font's own FontDescriptor).
        try:
            fd = font.get('/FontDescriptor', None)
            if fd is None:
                fd = font['/DescendantFonts'][0]['/FontDescriptor']
            for k in ('/FontFile2', '/FontFile', '/FontFile3'):
                if k in fd:
                    return fd[k].read_bytes()
        except Exception:
            return None
        return None

    def is_georgia_cid_font(self, font):
        try:
            return (font.get('/Subtype', None) == Name('/Type0')
                    and 'Georgia' in str(font.get('/BaseFont', '')))
        except Exception:
            return False

    def is_georgia_truetype_font(self, font):
        try:
            return (font.get('/Subtype', None) == Name('/TrueType')
                    and 'Georgia' in str(font.get('/BaseFont', '')))
        except Exception:
            return False

    def map_for_font(self, font):
        """Build a char map for a Georgia font object (subset-aware).

        Type0/CID fonts -> {cid(gid): char} (Identity-H, so gid == cid).
        Simple TrueType fonts -> {char_code: char}, derived from the embedded
        font's cmap (char code -> glyph) plus glyph-outline matching (glyph ->
        char).
        """
        if self.is_georgia_truetype_font(font):
            return self.code_map_for_font(font)
        if not self.is_georgia_cid_font(font):
            return None
        raw = self._extract_font_bytes(font)
        if raw is None:
            return None
        key = hashlib.sha256(raw).hexdigest()
        if key not in self.map_cache:
            self.map_cache[key] = self._build_map(raw, str(font.get('/BaseFont', 'cid')))
        return self.map_cache[key]

    def code_map_for_font(self, font):
        """Build {char_code: char} for a simple TrueType Georgia font.

        Uses the embedded font's cmap to translate a character code to its
        glyph, then glyph-outline matching to translate that glyph to a real
        Unicode character.
        """
        raw = self._extract_font_bytes(font)
        if raw is None:
            return None
        key = hashlib.sha256(raw).hexdigest()
        if key not in self._code_cache:
            gmap = self._build_map(raw, str(font.get('/BaseFont', 'tt')))  # gid->char
            code2char = {}
            try:
                from fontTools.ttLib import TTFont
                import os as _os
                tmp = '_subset_codemap.ttf'
                with open(tmp, 'wb') as fh:
                    fh.write(raw)
                tf = TTFont(tmp)
                _os.remove(tmp)
                go = tf.getGlyphOrder()
                tables = tf['cmap'].tables
                best = None
                for t in tables:
                    if t.platformID == 1 and t.platEncID == 0:
                        best = t
                        break
                if best is None and tables:
                    best = tables[0]
                if best is not None:
                    for code, gname in best.cmap.items():
                        if gname in go:
                            gid = go.index(gname)
                            ch = gmap.get(gid, '')
                            if ch:
                                code2char[code] = ch
            except Exception:
                code2char = {}
            self._code_cache[key] = code2char
        return self._code_cache[key]

    def widths_for_font(self, font):
        """Return {code_or_gid: width_in_1000_units} for a font object.

        CID fonts: parse the /W widths of the descendant.
        Simple fonts: parse /FirstChar + /Widths.
        """
        try:
            subtype = font.get('/Subtype', None)
            if subtype == Name('/Type0'):
                df = font['/DescendantFonts'][0]
                w = df.get('/W')
                if w is None:
                    return {}
                return parse_cid_w(list(w))
            # simple font
            first = int(font.get('/FirstChar', 0))
            widths = font.get('/Widths')
            if widths is None:
                return {}
            return {first + i: int(x) for i, x in enumerate(widths)}
        except Exception:
            return {}

    def load_font_for(self, font_name, page):
        """Extract embedded font bytes for a Georgia Type0 font on `page`."""
        try:
            fonts = page['/Resources']['/Font']
            f = fonts.get(pikepdf.Name(font_name))
            if f is None or not (self.is_georgia_cid_font(f)
                                 or self.is_georgia_truetype_font(f)):
                return None
            return self._extract_font_bytes(f)
        except Exception:
            return None
        return None

    def resolve(self, font_name, page):
        """Return gid->char map for the subset font, building on first use."""
        if not self.pdf:
            return None
        try:
            f = page['/Resources']['/Font'].get(pikepdf.Name(font_name))
        except Exception:
            return None
        if f is None:
            return None
        return self.map_for_font(f)

    def _build_map(self, raw, font_name):
        tmp = f'_subset_{font_name.replace("/", "")}.ttf'
        with open(tmp, 'wb') as fh:
            fh.write(raw)
        sub_face = load_face(tmp)
        gmap = {}
        for gid in range(1, sub_face.num_glyphs):
            pred, score = self.lib.match(sub_face, gid)
            if pred is not None:
                gmap[gid] = chr(pred)
        if self.verbose:
            print(f'  [subset {font_name}] built map for {len(gmap)} glyphs '
                  f'(num_glyphs={sub_face.num_glyphs})')
        os.remove(tmp)
        return gmap
