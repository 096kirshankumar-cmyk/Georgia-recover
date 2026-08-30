"""Glyph-outline matching to recover GID->Unicode for subsetted, no-cmap fonts.

Renders each glyph by index with FreeType (no hinting), normalizes to a fixed
binary grid, and matches against a reference font's glyphs (built from its cmap)
using IoU on the ink bitmaps.
"""
import freetype
import numpy as np

GRID = 40  # normalized grid size for shape comparison


def load_face(path):
    face = freetype.Face(path)
    face.set_char_size(64 * 64)  # large size, we crop to ink anyway
    return face


def _ink_bitmap(bitmap):
    """Return cropped binary mask (numpy bool) of a rendered glyph bitmap."""
    w = bitmap.width
    rows = bitmap.rows
    if w <= 0 or rows <= 0:
        return None
    buf = bitmap.buffer
    if isinstance(buf, (list, tuple)):
        data = np.array(buf, dtype=np.uint8)
        data = data.reshape(rows, w)
    else:
        pitch = bitmap.pitch
        data = np.frombuffer(buf, dtype=np.uint8).reshape(rows, abs(pitch))
        if pitch < 0:
            data = data[:, :w]
        else:
            data = data[:, :w]
    mask = data > 60
    # crop to ink bounding box
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    x0, x1 = xs.min(), xs.max() + 1
    y0, y1 = ys.min(), ys.max() + 1
    return mask[y0:y1, x0:x1]


def normalize(mask):
    """Pad a cropped binary mask to a centered GRIDxGRID square, preserving aspect."""
    h, w = mask.shape
    scale = GRID / max(h, w)
    nh = max(1, round(h * scale))
    nw = max(1, round(w * scale))
    # simple nearest resize
    ys = (np.arange(nh) * h / nh).astype(int)
    xs = (np.arange(nw) * w / nw).astype(int)
    small = mask[np.ix_(ys, xs)]
    out = np.zeros((GRID, GRID), dtype=bool)
    y0 = (GRID - nh) // 2
    x0 = (GRID - nw) // 2
    out[y0:y0 + nh, x0:x0 + nw] = small
    return out


def glyph_bitmap(face, glyph_index):
    face.load_glyph(glyph_index, freetype.FT_LOAD_RENDER | freetype.FT_LOAD_NO_HINTING)
    mask = _ink_bitmap(face.glyph.bitmap)
    if mask is None:
        return None
    return normalize(mask)


def iou(a, b):
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    if union == 0:
        return 0.0
    return inter / union


class ReferenceLibrary:
    """Map subset GID -> best-matching unicode using the reference font."""

    def __init__(self, ref_path, chars=None):
        self.face = load_face(ref_path)
        self.glyph_to_uni = {}
        self.lib = []  # (uni, normalized mask)
        cmap = {}
        # get cmap via fontTools
        from fontTools.ttLib import TTFont
        tt = TTFont(ref_path)
        cmap = tt["cmap"].getBestCmap()
        if chars is None:
            chars = sorted(cmap.keys())
        for cp in chars:
            # map unicode->glyph via freetype
            idx = self.face.get_char_index(cp)
            if idx == 0:
                continue
            b = glyph_bitmap(self.face, idx)
            if b is not None:
                self.lib.append((cp, b))
        self.lib_cp = [c for c, _ in self.lib]
        self.lib_masks = np.stack([m for _, m in self.lib])
        print(f"[ref] indexed {len(self.lib)} glyphs")

    def match(self, sub_face, gid):
        b = glyph_bitmap(sub_face, gid)
        if b is None:
            return None, 0.0
        # IoU against all ref masks
        inter = np.logical_and(self.lib_masks, b[None]).sum(axis=(1, 2))
        union = np.logical_or(self.lib_masks, b[None]).sum(axis=(1, 2))
        union[union == 0] = 1
        scores = inter / union
        k = int(np.argmax(scores))
        return self.lib_cp[k], float(scores[k])
