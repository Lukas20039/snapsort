import os
import shutil
import struct
import threading

import pytest
from conftest import DATE, THUMB, exif_app1, make_tiff, seg
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from test_exif import DISPLAY

from snapsort import exif, imaging

COLORS = [QColor(c) for c in ("#ff0000", "#00ff00", "#0000ff", "#ffff00", "#00ffff", "#ff00ff")]
B = 16  # block size: a multiple of the JPEG MCU keeps colours clean


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch, qapp):
    monkeypatch.setattr(imaging, "CACHE", tmp_path / "thumbs")


def noise(w, h):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    for y in range(h):
        for x in range(w):
            img.setPixelColor(x, y, QColor((x * 37 + y * 11) % 256, (x * 7 + y * 53) % 256, (x * y) % 256))
    return img


def blocks(grid):
    rows, cols = len(grid), len(grid[0])
    img = QImage(cols * B, rows * B, QImage.Format.Format_RGB32)
    for r, row in enumerate(grid):
        for c, v in enumerate(row):
            for y in range(B):
                for x in range(B):
                    img.setPixelColor(c * B + x, r * B + y, COLORS[v - 1])
    return img


def read_blocks(img):
    def nearest(col):
        return 1 + min(range(6), key=lambda i: sum((a - b) ** 2 for a, b in zip(
            COLORS[i].getRgb()[:3], col.getRgb()[:3])))
    return tuple(tuple(nearest(img.pixelColor(c * B + B // 2, r * B + B // 2))
                       for c in range(img.width() // B)) for r in range(img.height() // B))


def with_exif(jpeg: bytes, app1: bytes) -> bytes:
    app0_end = exif.segments(jpeg)[0][2]
    return jpeg[:app0_end] + app1 + jpeg[app0_end:]


def jpeg(tmp_path, img, app1=b"", name="a.jpg"):
    p = tmp_path / name
    p.write_bytes(with_exif(imaging.encode(img, "jpg", 100), app1))
    return p


def mean_diff(a, b):
    assert a.size() == b.size()
    tot = 0
    for y in range(a.height()):
        for x in range(a.width()):
            ca, cb = a.pixelColor(x, y), b.pixelColor(x, y)
            tot += abs(ca.red() - cb.red()) + abs(ca.green() - cb.green()) + abs(ca.blue() - cb.blue())
    return tot / (3 * a.width() * a.height())


def test_decode_applies_orientation(tmp_path):
    p = jpeg(tmp_path, noise(64, 32), exif_app1(orientation=6))
    assert imaging.decode_scaled(p).size() == QSize(32, 64)


def test_decode_scaled_fits_target_after_transpose(tmp_path):
    p = jpeg(tmp_path, noise(64, 32), exif_app1(orientation=6))
    img = imaging.decode_scaled(p, QSize(16, 16))
    assert img.width() <= 16 and img.height() <= 16 and img.height() > img.width()


def test_compose_table_agrees_with_qt(tmp_path):
    grid = ((1, 2, 3), (4, 5, 6))
    for v in range(1, 9):
        p = jpeg(tmp_path, blocks(grid), exif_app1(orientation=v))
        assert read_blocks(imaging.decode_scaled(p)) == DISPLAY[v](grid), v


def test_transform_patches_only_orientation_bytes(tmp_path):
    p = jpeg(tmp_path, noise(32, 16), exif_app1(orientation=3))
    before = p.read_bytes()
    _, off, bo = exif.orientation_patch(before)
    assert imaging.transform_file(p, "cw") == before
    after = p.read_bytes()
    diff = [i for i in range(len(before)) if before[i] != after[i]]
    assert len(after) == len(before) and set(diff) <= {off, off + 1}
    assert exif.get_orientation(after) == exif.compose(3, "cw")


def test_transform_without_exif_inserts_orientation(tmp_path):
    p = jpeg(tmp_path, noise(33, 17))  # not MCU-aligned: jpegtran -perfect would fail
    before = p.read_bytes()
    imaging.transform_file(p, "cw")
    out = p.read_bytes()
    assert exif.get_orientation(out) == 6
    assert out.endswith(before[exif.segments(before)[0][2]:])  # image data untouched
    assert imaging.decode_scaled(p).size() == QSize(17, 33)


@pytest.mark.skipif(shutil.which("jpegtran") is None, reason="jpegtran not installed")
def test_transform_lossless_jpegtran(tmp_path, monkeypatch):
    empty_ifd0 = seg(0xE1, b"Exif\0\0MM\0*\0\0\0\x08\0\0\0\0\0\0")  # Exif without Orientation
    p = jpeg(tmp_path, noise(32, 16), empty_ifd0)
    expected = imaging.apply_op(imaging.decode_scaled(p), "cw")
    monkeypatch.setattr(imaging, "_write_reencoded", None)  # must not fall back
    imaging.transform_file(p, "cw")
    got = imaging.decode_scaled(p)
    assert got.size() == QSize(16, 32) and mean_diff(got, expected) < 2


def test_transform_reencode_fallback_keeps_date(tmp_path):
    tiff = make_tiff(orientation=6)
    tiff = tiff[:10] + struct.pack("<H", 0x0131) + tiff[12:]  # Orientation entry -> Software
    p = jpeg(tmp_path, noise(33, 17), seg(0xE1, b"Exif\0\0" + tiff))
    assert exif.orientation_patch(p.read_bytes()) is None
    imaging.transform_file(p, "cw")
    out = p.read_bytes()
    assert imaging.decode_scaled(p).size() == QSize(17, 33)
    assert DATE in out and THUMB not in out


def test_transform_png_pixel_exact(tmp_path):
    p = tmp_path / "a.png"
    src = noise(7, 5)
    p.write_bytes(imaging.encode(src, "png"))
    imaging.transform_file(p, "cw")
    got = imaging.decode_scaled(p).convertToFormat(QImage.Format.Format_RGB32)
    assert got == imaging.apply_op(src, "cw").convertToFormat(QImage.Format.Format_RGB32)


GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00"
       b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")


def test_transform_gif_refused(tmp_path):
    p = tmp_path / "a.gif"
    p.write_bytes(GIF)
    with pytest.raises(OSError):
        imaging.transform_file(p, "cw")
    assert p.read_bytes() == GIF


def test_save_edited_resets_orientation_keeps_metadata(tmp_path):
    p = jpeg(tmp_path, noise(32, 16), exif_app1(orientation=6))
    before = p.read_bytes()
    img = imaging.decode_scaled(p)
    assert imaging.save_edited(p, img) == before
    out = p.read_bytes()
    assert exif.get_orientation(out) == 1 and DATE in out and THUMB not in out
    assert imaging.decode_scaled(p).size() == img.size()


def test_thumb_key_tracks_mtime(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(imaging.encode(noise(4, 4), "png"))
    k1 = imaging.thumb_key(p)
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert imaging.thumb_key(p) != k1


def test_make_thumb_reads_cache(tmp_path, monkeypatch):
    p = tmp_path / "a.png"
    p.write_bytes(imaging.encode(noise(300, 100), "png"))
    first = imaging.make_thumb(p)
    assert imaging.thumb_key(p).exists() and first.width() == imaging.THUMB

    def boom(*a, **k):
        raise AssertionError("decoded again")
    monkeypatch.setattr(imaging, "decode_scaled", boom)
    assert imaging.make_thumb(p).size() == first.size()


def wait_for(cond, ms=5000):
    for _ in range(ms // 10):
        if cond():
            return True
        QTest.qWait(10)
    return cond()


def pngs(tmp_path, n, w=40, h=30):
    out = []
    for i in range(n):
        p = tmp_path / f"{i}.png"
        p.write_bytes(imaging.encode(noise(w, h), "png"))
        out.append(str(p))
    return out


def test_loader_prefetch_fills_cache(tmp_path):
    loader = imaging.Loader()
    paths = pngs(tmp_path, 3)
    ready = []
    loader.imageReady.connect(lambda p, img: ready.append(p))
    loader.prefetch(paths, QSize(800, 600))
    assert wait_for(lambda: len(ready) == 3)
    assert all(loader.cached(p, QSize(800, 600)) is not None for p in paths)


def test_loader_lru_evicts_least_recent(tmp_path):
    loader = imaging.Loader()
    a, b, c = pngs(tmp_path, 3)
    img = imaging.decode_scaled(a)
    img.setText("full", "1")
    loader.cache_limit = 2 * img.sizeInBytes()
    loader._decoded.emit(a, img, 0)
    loader._decoded.emit(b, img, 0)
    loader.cached(a, QSize(1, 1))  # touch a: b is now the oldest
    loader._decoded.emit(c, img, 0)
    assert list(loader.cache) == [a, c] and loader.cache_bytes == 2 * img.sizeInBytes()


def test_loader_invalidate(tmp_path):
    loader = imaging.Loader()
    (a,) = pngs(tmp_path, 1)
    img = imaging.decode_scaled(a)
    loader._decoded.emit(a, img, 0)
    loader._thumbed.emit(a, img, 0)
    loader.invalidate(a)
    assert a not in loader.cache and a not in loader.thumbs and loader.cache_bytes == 0


def test_loader_invalidate_discards_inflight_decode(tmp_path, monkeypatch):
    loader = imaging.Loader()
    (a,) = pngs(tmp_path, 1)
    gate, real = threading.Event(), imaging.decode_scaled
    monkeypatch.setattr(imaging, "decode_scaled", lambda *args: (gate.wait(5), real(*args))[1])
    loader.prefetch([a], QSize(800, 600))
    QTest.qWait(50)  # decode is running, blocked on the gate
    loader.invalidate(a)  # e.g. the file was just rotated
    gate.set()
    loader.decode_pool.waitForDone()
    QTest.qWait(50)
    assert a not in loader.cache


def test_make_thumb_survives_readonly_cache(tmp_path, monkeypatch):
    (a,) = pngs(tmp_path, 1)
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    monkeypatch.setattr(imaging, "CACHE", ro / "thumbs")
    monkeypatch.setattr(imaging, "atomic_write", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "ro")))
    assert not imaging.make_thumb(a).isNull()
