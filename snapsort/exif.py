"""Byte-level JPEG/EXIF helpers. Pure Python, never raises on malformed input."""
import re
import struct

_NO_LENGTH = {0x01, 0xD8} | set(range(0xD0, 0xD8))
EXIF_HDR = b"Exif\0\0"
XMP_HDRS = (b"http://ns.adobe.com/xap/1.0/\0", b"http://ns.adobe.com/xmp/extension\0")
ICC_HDR = b"ICC_PROFILE\0"
IPTC_HDR = b"Photoshop 3.0\0"


def segments(data: bytes) -> list[tuple[int, int, int]]:
    """(marker, start, end) for each header segment; stops before SOS/EOI."""
    out = []
    if data[:2] != b"\xFF\xD8":
        return out
    pos, n = 2, len(data)
    while pos < n:
        if data[pos] != 0xFF:
            return out
        start = pos
        while pos < n and data[pos] == 0xFF:
            pos += 1
        if pos >= n:
            return out
        marker = data[pos]
        pos += 1
        if marker in (0xDA, 0xD9):
            return out
        if marker in _NO_LENGTH:
            out.append((marker, start, pos))
            continue
        if pos + 2 > n:
            return out
        end = pos + struct.unpack_from(">H", data, pos)[0]
        if end > n or end < pos + 2:
            return out
        out.append((marker, start, end))
        pos = end
    return out


def _payload_start(data, start):
    # skip fill bytes, marker and length
    p = start
    while data[p] == 0xFF:
        p += 1
    return p + 3


def _is(data, seg, marker, hdr):
    m, s, e = seg
    if m != marker:
        return False
    p = _payload_start(data, s)
    return data[p:p + len(hdr)] == hdr


def _exif_segment(data):
    for seg in segments(data):
        if _is(data, seg, 0xE1, EXIF_HDR):
            return seg
    return None


def _ifd(data, t, end, bo, off):
    """Entries [(tag, type, count, entry_pos)] and next-ifd pos, or None."""
    ifd = t + off
    if off < 8 or ifd + 2 > end:
        return None
    n = struct.unpack_from(bo + "H", data, ifd)[0]
    nxt = ifd + 2 + 12 * n
    if nxt + 4 > end:
        return None
    entries = []
    for i in range(n):
        e = ifd + 2 + 12 * i
        tag, typ, cnt = struct.unpack_from(bo + "HHI", data, e)
        entries.append((tag, typ, cnt, e))
    return entries, nxt


def _tiff(data, seg):
    """(tiff_start, seg_end, byteorder) or None."""
    _, s, end = seg
    t = _payload_start(data, s) + len(EXIF_HDR)
    if t + 8 > end:
        return None
    bo = {b"II": "<", b"MM": ">"}.get(data[t:t + 2])
    if not bo or struct.unpack_from(bo + "H", data, t + 2)[0] != 42:
        return None
    return t, end, bo


def orientation_patch(data: bytes):
    """(value, abs_offset, byteorder) of IFD0 Orientation, or None."""
    seg = _exif_segment(data)
    tiff = seg and _tiff(data, seg)
    if not tiff:
        return None
    t, end, bo = tiff
    ifd = _ifd(data, t, end, bo, struct.unpack_from(bo + "I", data, t + 4)[0])
    if not ifd:
        return None
    for tag, typ, cnt, e in ifd[0]:
        if tag == 0x0112 and typ == 3 and cnt == 1:
            v = struct.unpack_from(bo + "H", data, e + 8)[0]
            return (v if 1 <= v <= 8 else 1), e + 8, bo
    return None


def get_orientation(data: bytes) -> int:
    p = orientation_patch(data)
    return p[0] if p else 1


# EXIF orientation -> (k, f): mirror horizontally if f, then rotate k*90deg clockwise.
_DEC = {1: (0, 0), 2: (0, 1), 3: (2, 0), 4: (2, 1), 5: (3, 1), 6: (1, 0), 7: (1, 1), 8: (3, 0)}
_ENC = {v: k for k, v in _DEC.items()}
OPS = {"cw": (1, 0), "ccw": (3, 0), "fliph": (0, 1), "flipv": (2, 1)}


def compose(orientation: int, op: str) -> int:
    ka, fa = _DEC.get(orientation, (0, 0))
    kb, fb = OPS[op]
    return _ENC[((kb + (-ka if fb else ka)) % 4, fa ^ fb)]


def _clean_exif(data: bytes, seg) -> bytes:
    """Exif segment bytes with orientation=1 and the IFD1 thumbnail zeroed."""
    _, s, e = seg
    buf = bytearray(data[s:e])
    tiff = _tiff(data, seg)
    if not tiff:
        return bytes(buf)
    t, end, bo = tiff
    rel = lambda p: p - s  # noqa: E731
    ifd0 = _ifd(data, t, end, bo, struct.unpack_from(bo + "I", data, t + 4)[0])
    if not ifd0:
        return bytes(buf)
    entries, nxt = ifd0
    for tag, typ, cnt, ep in entries:
        if tag == 0x0112 and typ == 3 and cnt == 1:
            struct.pack_into(bo + "H", buf, rel(ep + 8), 1)
    ifd1 = _ifd(data, t, end, bo, struct.unpack_from(bo + "I", data, nxt)[0])
    if ifd1:
        vals = {tag: struct.unpack_from(bo + "I", data, ep + 8)[0]
                for tag, typ, cnt, ep in ifd1[0] if tag in (0x0201, 0x0202) and cnt == 1}
        if 0x0201 in vals and 0x0202 in vals:
            a = t + vals[0x0201]
            b = min(a + vals[0x0202], end)
            if a < b:
                buf[rel(a):rel(b)] = bytes(b - a)
    struct.pack_into(bo + "I", buf, rel(nxt), 0)
    return bytes(buf)


_XMP_ORIENT = re.compile(rb'(tiff:Orientation="|<tiff:Orientation>)[1-8]')


def splice_metadata(new_jpeg: bytes, original: bytes) -> bytes:
    """Re-attach original EXIF/XMP/ICC/IPTC to a freshly encoded JPEG (orientation reset to 1)."""
    keep = []
    for seg in segments(original):
        m, s, e = seg
        if _is(original, seg, 0xE1, EXIF_HDR):
            keep.append(_clean_exif(original, seg))
        elif any(_is(original, seg, 0xE1, h) for h in XMP_HDRS):
            keep.append(_XMP_ORIENT.sub(lambda mo: mo.group(1) + b"1", original[s:e]))
        elif _is(original, seg, 0xE2, ICC_HDR) or _is(original, seg, 0xED, IPTC_HDR):
            keep.append(original[s:e])
    new_segs = segments(new_jpeg)
    if not new_segs or not keep:
        return new_jpeg
    app0 = [new_jpeg[s:e] for m, s, e in new_segs if m == 0xE0]
    rest_start = new_segs[-1][2]
    rest = [new_jpeg[s:e] for m, s, e in new_segs if m not in (0xE0, 0xE1, 0xE2, 0xED)]
    return b"".join([b"\xFF\xD8", *app0, *keep, *rest, new_jpeg[rest_start:]])
