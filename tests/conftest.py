import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

DATE = b"2024:05:01 12:34:56\0"
THUMB = b"\xAB" * 16


def make_tiff(orientation=6, bo="<", with_thumb=True):
    """Minimal TIFF: IFD0 (Orientation, DateTime), optional IFD1 thumbnail."""
    head = (b"II" if bo == "<" else b"MM") + struct.pack(bo + "HI", 42, 8)
    ifd0_len = 2 + 12 * 2 + 4
    date_off = 8 + ifd0_len
    ifd1_off = date_off + len(DATE)
    ifd1_len = 2 + 12 * 2 + 4
    thumb_off = ifd1_off + ifd1_len
    ifd0 = struct.pack(bo + "H", 2)
    ifd0 += struct.pack(bo + "HHI", 0x0112, 3, 1) + struct.pack(bo + "H", orientation) + b"\0\0"
    ifd0 += struct.pack(bo + "HHII", 0x0132, 2, len(DATE), date_off)
    ifd0 += struct.pack(bo + "I", ifd1_off if with_thumb else 0)
    out = head + ifd0 + DATE
    if with_thumb:
        ifd1 = struct.pack(bo + "H", 2)
        ifd1 += struct.pack(bo + "HHII", 0x0201, 4, 1, thumb_off)
        ifd1 += struct.pack(bo + "HHII", 0x0202, 4, 1, len(THUMB))
        ifd1 += struct.pack(bo + "I", 0)
        out += ifd1 + THUMB
    return out


def seg(marker, payload):
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def exif_app1(**kw):
    return seg(0xE1, b"Exif\0\0" + make_tiff(**kw))


def fake_jpeg(*segs):
    return b"\xFF\xD8" + seg(0xE0, b"JFIF\0\1\1\0\0\1\0\1\0\0") + b"".join(segs) + \
        seg(0xDB, b"\0" * 65) + seg(0xDA, b"\0" * 10) + b"SCANDATA\xFF\xD9"


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])
